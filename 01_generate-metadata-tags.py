"""
Generate metadata tags and upload documents to S3.

Reads Knowledge Base Metadata.xlsx and orchestrates:
- Generating .metadata.json files for all documents (HTML and PDF)
- Uploading paired .md + .metadata.json files to S3

Assumes:
- HTML: 00_html_to_markdown.py has already been run (markdown + urls_config.json exist)
- PDF:  DATA_FOLDER is set in .env pointing to the Box Drive root folder
"""

import json
from pathlib import Path

import boto3
import pandas as pd
from docling.document_converter import DocumentConverter

from config import (
    BUCKET_NAME, REGION, KB_CONFIGS, PROJECT_ROOT,
    DATA_FOLDER, METADATA_PATH
)
from utils.utils import derive_subfolder, extract_year, print_pipeline_summary, slugify

URLS_CONFIG_PATH = PROJECT_ROOT/"urls_config.json"
MD_OUTPUT_DIR = PROJECT_ROOT/"data"/"md"


# ---------------------------------------------------------------------------
# Metadata generation
# ---------------------------------------------------------------------------

def _group_and_write_metadata(df, md_output_dir, skip_existing=False):
    """Group rows by document and write .metadata.json files.

    Handles cases where one document maps to multiple predictors/pillars
    by collapsing duplicate rows and capturing all unique values.

    Shared logic for both HTML and PDF metadata generation. Expects a
    DataFrame with columns: Document Name, Document URL, Information Source,
    Information Type, Publication Date, Pillar, Predictor, subfolder, filename.

    Args:
        skip_existing: If True, skip documents whose .metadata.json already exists.
    """
    # Group by URL + filename so that split framework docs (same URL, different
    # filenames) each get their own metadata file rather than being collapsed.
    # Multi-predictor docs (same URL, same filename) are still aggregated correctly.
    first_cols = {c: "first" for c in df.columns
                  if c not in ("Pillar", "Predictor")}
    grouped = (
        df
        .groupby(["Document URL", "filename"], sort=False)
        .agg({**first_cols,
               "Pillar": lambda x: list(dict.fromkeys(x)),
               "Predictor": lambda x: list(dict.fromkeys(x))})
        .reset_index(drop=True)
    )

    matched = 0
    skipped = 0
    for _, row in grouped.iterrows():
        md_filepath = md_output_dir / row["subfolder"] / row["filename"]
        metadata_file = md_filepath.parent / f"{md_filepath.name}.metadata.json"

        if skip_existing and metadata_file.exists():
            skipped += 1
            continue

        metadata = {}
        metadata['document_name'] = row['Document Name']
        metadata['url'] = row['Document URL']
        metadata['source'] = row['Information Source']
        metadata['document_type'] = row['Information Type']
        year = extract_year(row['Publication Date'])
        if year is not None:
            metadata['publication_year'] = year
        for i, pillar in enumerate(row['Pillar'], 1):
            metadata[f'pillar_{i}'] = pillar
        for i, predictor in enumerate(row['Predictor'], 1):
            metadata[f'predictor_{i}'] = predictor

        metadata_dict = {'metadataAttributes': metadata}
        metadata_file.write_text(json.dumps(metadata_dict, indent=2))
        matched += 1

    return matched, skipped


def generate_html_metadata_files(metadata_path=METADATA_PATH,
                                  urls_config_path=URLS_CONFIG_PATH,
                                  md_output_dir=MD_OUTPUT_DIR,
                                  skip_existing=False):
    """Generate .metadata.json files for HTML-sourced markdown.

    Joins the metadata spreadsheet with urls_config.json (which has the
    URL->filename mapping from scraping) to match each document to its
    .md file on disk.
    """
    metadata_df = pd.read_excel(metadata_path)
    with open(urls_config_path) as f:
        config_entries = json.load(f)["urls"]
    config_df = pd.DataFrame(config_entries)

    html_df = metadata_df[metadata_df["File Type"] == "HTML"]
    config_with_files = config_df[config_df["filename"].notna()].drop_duplicates(
        subset=["url", "filename"]
    )
    merged = html_df.merge(config_with_files, left_on="Document URL",
                           right_on="url", how="inner")

    # For split framework docs, the config has a document_type field that must
    # match the spreadsheet's Information Type to avoid cross-product duplicates.
    # Non-split docs have no document_type in the config, so they pass through.
    if "document_type" in merged.columns:
        is_split = merged["document_type"].notna()
        types_match = merged["document_type"] == merged["Information Type"]
        merged = merged[~is_split | types_match]

    generated, skipped = _group_and_write_metadata(merged, md_output_dir, skip_existing)
    print(f"Generated {generated} HTML metadata files ({skipped} skipped)")


def generate_pdf_metadata_files(metadata_path=METADATA_PATH,
                                md_output_dir=MD_OUTPUT_DIR,
                                skip_existing=False):
    """Generate .metadata.json files for PDF-sourced markdown.

    Reads PDF rows from the metadata spreadsheet directly — no urls_config
    join needed since filenames come from the spreadsheet's File Name column.

    Uses the same _group_and_write_metadata() helper as the HTML pipeline,
    so all metadata tags (pillar_1/2, predictor_1/2, etc.) are handled
    identically.
    """
    metadata_df = pd.read_excel(metadata_path)
    pdf_df = metadata_df[metadata_df["File Type"] == "PDF"].copy()

    pdf_with_files = pdf_df[pdf_df["File Name"].notna()].copy()
    if pdf_with_files.empty:
        print("No PDF rows with File Name — skipping PDF metadata generation")
        return

    # Derive subfolder from Information Source (same logic as HTML)
    pdf_with_files["subfolder"] = pdf_with_files["Information Source"].apply(
        derive_subfolder
    )
    # Clean and derive .md filename from File Name (strip special chars, add extension)
    pdf_with_files["filename"] = pdf_with_files["File Name"].apply(
        lambda f: slugify(f) + ".md"
    )

    generated, skipped = _group_and_write_metadata(pdf_with_files, md_output_dir, skip_existing)
    print(f"Generated {generated} PDF metadata files ({skipped} skipped)")


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def upload_md_files_to_s3(md_dir=MD_OUTPUT_DIR,
                          s3_prefix=KB_CONFIGS["default-md"]["documents_prefix"],
                          max_file_size=1_000_000):
    """Sync local .md + .metadata.json files to S3, replacing whatever is there.

    Uploads all paired .md + .metadata.json files from md_dir, then deletes
    any S3 objects under s3_prefix that no longer exist locally. This ensures
    S3 is an exact mirror of the local md directory.

    Args:
        md_dir: Local directory containing markdown files (e.g., "data/md/")
        s3_prefix: S3 key prefix (default: "documents-md/" from config)
    """
    s3 = boto3.client("s3", region_name=REGION)
    md_dir = Path(md_dir)

    # Upload all paired files and track which S3 keys we uploaded
    uploaded_keys = set()
    uploaded = 0
    skipped_size = []
    for md_file in md_dir.rglob('*.md'):
        metadata_file = Path(str(md_file) + '.metadata.json')
        if not metadata_file.exists():
            print(f'No metadata file for {str(md_file)}. Skipping to next document...')
            continue
        file_size = md_file.stat().st_size
        if file_size > max_file_size:
            skipped_size.append((md_file.relative_to(md_dir), file_size))
            continue
        s3_key = s3_prefix + str(md_file.relative_to(md_dir))
        meta_s3_key = s3_key + '.metadata.json'

        s3.upload_file(str(md_file), BUCKET_NAME, s3_key)
        s3.upload_file(str(metadata_file), BUCKET_NAME, meta_s3_key)
        uploaded_keys.add(s3_key)
        uploaded_keys.add(meta_s3_key)
        uploaded += 1

    if skipped_size:
        print(f"Skipped {len(skipped_size)} files exceeding {max_file_size/1e6:.0f}MB limit:")
        for path, size in skipped_size:
            print(f"  {path} ({size/1e6:.1f}MB)")

    print(f"Uploaded {uploaded} file pairs to s3://{BUCKET_NAME}/{s3_prefix}")

    # Delete any S3 objects under s3_prefix that weren't just uploaded
    deleted = 0
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=s3_prefix):
        for obj in page.get('Contents', []):
            if obj['Key'] not in uploaded_keys:
                s3.delete_object(Bucket=BUCKET_NAME, Key=obj['Key'])
                deleted += 1

    if deleted:
        print(f"Deleted {deleted} stale files from s3://{BUCKET_NAME}/{s3_prefix}")


# ---------------------------------------------------------------------------
# PDF conversion
# ---------------------------------------------------------------------------

def _find_pdf(file_name, data_folder=DATA_FOLDER):
    """Recursively search DATA_FOLDER for a PDF matching file_name.

    Uses case-insensitive extension matching to handle .pdf/.PDF/.Pdf etc.

    Args:
        file_name: Base filename without extension (from spreadsheet)

    Returns:
        Path to the PDF, or None if not found
    """
    target = slugify(file_name)
    for path in data_folder.rglob("*.[pP][dD][fF]"):
        if slugify(path.stem) == target:
            return path
    print(f"  PDF not found for: {target}.pdf")
    return None


def convert_pdf_to_md(pdf_path, output_path):
    """Convert a single PDF file to Markdown format using docling.

    Args:
        pdf_path: Full path to the source PDF file
        output_path: Full path for the output .md file

    Returns:
        The output path on success, or None on failure
    """
    try:
        convertor = DocumentConverter()
        result = convertor.convert(str(pdf_path))
        markdown_text = result.document.export_to_markdown()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown_text, encoding="utf-8")
        return output_path
    except Exception as e:
        print(f"  Failed to convert {pdf_path.name}: {e}")
        return None


def convert_all_pdfs(metadata_path=METADATA_PATH,
                     data_folder=DATA_FOLDER,
                     md_output_dir=MD_OUTPUT_DIR):
    """Find and convert all PDFs listed in the spreadsheet to markdown.

    Reads File Name from the spreadsheet, locates each PDF in data_folder
    via recursive search, converts via docling, and saves to
    md_output_dir/{subfolder}/{sanitized_name}.md.
    """
    if not data_folder.exists():
        print(f"data_folder not found: {data_folder}")
        print("Set DATA_FOLDER in .env to your Box Drive path")
        return

    metadata_df = pd.read_excel(metadata_path)
    pdf_df = metadata_df[metadata_df["File Type"] == "PDF"]
    pdf_with_files = pdf_df[pdf_df["File Name"].notna()]

    converted = 0
    skipped = 0
    for _, row in pdf_with_files.iterrows():
        raw_name = row["File Name"]
        clean_name = slugify(raw_name)

        # Derive output subfolder from Information Source
        subfolder = derive_subfolder(row["Information Source"])

        output_path = md_output_dir / subfolder / f"{clean_name}.md"

        # Skip if already converted
        if output_path.exists():
            skipped += 1
            continue

        pdf_path = _find_pdf(raw_name, data_folder)
        if pdf_path is None:
            continue

        print(f"  Converting: {pdf_path.name} -> {output_path}")
        if convert_pdf_to_md(pdf_path, output_path):
            converted += 1

    print(f"Converted {converted} PDFs to markdown ({skipped} already existed)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip documents whose output files already exist")
    parser.add_argument("--sync-s3", action="store_true",
                        help="Upload all paired files to S3 and delete stale remote files")
    args = parser.parse_args()

    # --- HTML metadata ---
    generate_html_metadata_files(skip_existing=args.skip_existing)

    # --- PDF: convert then tag ---
    convert_all_pdfs()
    generate_pdf_metadata_files(skip_existing=args.skip_existing)

    # --- Pipeline summary ---
    print_pipeline_summary(METADATA_PATH, MD_OUTPUT_DIR, DATA_FOLDER,
                           urls_config_path=URLS_CONFIG_PATH)

    # --- Upload all paired files to S3 ---
    if args.sync_s3:
        upload_md_files_to_s3()
