"""Shared utility functions used across pipeline scripts."""

import json
import re
from pathlib import Path
import boto3


def slugify(text, max_length=80):
    """Convert a string to a safe, lowercase filename.

    Strips special characters, replaces spaces/hyphens with underscores,
    and lowercases everything. Used for both HTML-derived titles and
    researcher-provided PDF filenames to ensure consistent naming.

    Example: "Living_wages_Flagstaff, AZ" -> "living_wages_flagstaff_az"

    Args:
        text: Human-readable string to slugify
        max_length: Maximum character length before truncation

    Returns:
        Lowercase string safe for use as a filename
    """
    text = str(text).lower().strip()
    text = text.replace('-', '_').replace(' ', '_')
    text = re.sub(r'[^a-z0-9_]+', '', text)
    text = re.sub(r'_+', '_', text).strip('_')
    if len(text) > max_length:
        text = text[:max_length].rsplit('_', 1)[0]

    return text


def derive_subfolder(source):
    """Map an Information Source value to its output subfolder name.

    Args:
        source: Information Source string from the metadata spreadsheet

    Returns:
        Subfolder name: 'urban_mobility' or 'other_academic'
    """
    source_lower = source.lower()
    if 'urban' in source_lower:
        return 'urban_mobility'
    else:
        return 'other_academic'


def print_pipeline_summary(metadata_path, md_output_dir, data_folder,
                           urls_config_path=None, output_dir="results"):
    """Write a pipeline summary to a timestamped file in results/.

    Reads the spreadsheet and compares against files on disk to produce a
    single recap of all documents grouped by status. For orphan metadata
    files (metadata exists but no .md), detects the reason programmatically.

    Args:
        metadata_path: Path to Knowledge Base Metadata.xlsx
        md_output_dir: Path to the md output directory (e.g., data/md/)
        data_folder: Path to Box Drive DATA_FOLDER (for PDF lookup)
        output_dir: Directory for the summary file (default: results/)
    """
    import pandas as pd
    from datetime import datetime

    metadata_df = pd.read_excel(metadata_path)
    md_output_dir = Path(md_output_dir)
    data_folder = Path(data_folder)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Spreadsheet totals ---
    type_counts = metadata_df["File Type"].value_counts(dropna=False)

    # --- Excluded categories (mutually exclusive by File Type) ---
    paywalled_html = metadata_df[
        (metadata_df["Paywall?"] == "Yes") & (metadata_df["File Type"] == "HTML")
    ]
    interactive = metadata_df[metadata_df["File Type"] == "Interactive"]
    other = metadata_df[metadata_df["File Type"] == "Other"]
    pdf_no_filename = metadata_df[
        (metadata_df["File Type"] == "PDF") & (metadata_df["File Name"].isna())
    ]

    # --- Files on disk ---
    md_files = sorted(md_output_dir.rglob("*.md"))
    metadata_files = [f for f in md_output_dir.rglob("*.metadata.json")
                      if f.name != "metadata_schema.json"]
    md_names = {f.name for f in md_files}
    meta_names = {f.name.replace(".metadata.json", "") for f in metadata_files}

    orphan_metadata = sorted(meta_names - md_names)
    missing_metadata = sorted(md_names - meta_names)

    # --- Oversized files ---
    oversized = [(f, f.stat().st_size) for f in md_files if f.stat().st_size > 1_000_000]

    # --- Diagnose orphan metadata reasons ---
    pdf_df = metadata_df[metadata_df["File Type"] == "PDF"]
    pdf_with_fn = pdf_df[pdf_df["File Name"].notna()].copy()
    pdf_with_fn["slug"] = pdf_with_fn["File Name"].apply(slugify)

    orphan_reasons = []
    for name in orphan_metadata:
        stem = name.replace(".md", "")
        matches = pdf_with_fn[pdf_with_fn["slug"] == stem]
        if matches.empty:
            orphan_reasons.append((name, "Not found in spreadsheet"))
            continue
        row = matches.iloc[0]
        # Check if the PDF exists on Box
        pdf_found = False
        if data_folder.exists():
            target = slugify(row["File Name"])
            for path in data_folder.rglob("*.[pP][dD][fF]"):
                if slugify(path.stem) == target:
                    pdf_found = True
                    break
        if pdf_found:
            orphan_reasons.append((name, "PDF exists on Box but conversion failed"))
        else:
            orphan_reasons.append((name, "PDF not found in DATA_FOLDER"))

    # --- Step 00: HTML scrape failures ---
    scrape_failures = []
    if urls_config_path and Path(urls_config_path).exists():
        with open(urls_config_path) as f:
            url_entries = json.load(f)["urls"]
        scrape_failures = [e["url"] for e in url_entries if "filename" not in e]

    # --- Step 01: PDF conversion stats ---
    pdf_on_disk = 0
    pdf_not_on_disk = 0
    for _, row in pdf_with_fn.iterrows():
        subfolder = derive_subfolder(row["Information Source"])
        md_path = md_output_dir / subfolder / (slugify(row["File Name"]) + ".md")
        if md_path.exists():
            pdf_on_disk += 1
        else:
            pdf_not_on_disk += 1

    # --- Build summary lines ---
    lines = []
    lines.append("=" * 60)
    lines.append(" Pipeline Summary")
    lines.append("=" * 60)

    # Spreadsheet totals
    lines.append(f"\nSpreadsheet ({len(metadata_df)} rows):")
    for file_type, count in type_counts.items():
        label = file_type if pd.notna(file_type) else "(no File Type)"
        lines.append(f"  {label}: {count}")

    # Excluded (all docs not flowing through the pipeline)
    excluded_total = (len(paywalled_html) + len(interactive) + len(other)
                      + len(pdf_no_filename))
    lines.append(f"\nExcluded from pipeline ({excluded_total} total):")
    lines.append(f"  Paywalled HTML: {len(paywalled_html)}")
    lines.append(f"  Interactive: {len(interactive)}")
    lines.append(f"  Other: {len(other)}")
    lines.append(f"  PDF without File Name: {len(pdf_no_filename)}")

    # On disk
    paired = len(md_names & meta_names)
    lines.append(f"\nOn disk:")
    lines.append(f"  Markdown files: {len(md_files)}")
    lines.append(f"  Metadata files: {len(metadata_files)}")
    lines.append(f"  Paired (.md + .metadata.json): {paired}")

    # Step 00 — HTML scraping
    if urls_config_path:
        total_urls = len(url_entries)
        scraped_ok = total_urls - len(scrape_failures)
        lines.append(f"\nStep 00 — HTML scraping:")
        lines.append(f"  URLs attempted: {total_urls}")
        lines.append(f"  Successfully scraped: {scraped_ok}")
        if scrape_failures:
            lines.append(f"  Failed to scrape ({len(scrape_failures)}):")
            for url in scrape_failures:
                lines.append(f"    - {url}")

    # Step 01 — PDF conversion
    lines.append(f"\nStep 01 — PDF conversion:")
    lines.append(f"  PDFs eligible (with File Name): {len(pdf_with_fn)}")
    lines.append(f"  Converted to markdown: {pdf_on_disk}")
    if pdf_not_on_disk:
        lines.append(f"  Not on disk: {pdf_not_on_disk}")

    # Gaps
    has_gaps = (orphan_reasons or missing_metadata or oversized
                or len(paywalled_html) or len(pdf_no_filename))
    if has_gaps:
        lines.append(f"\nGaps:")
        if paywalled_html.any(axis=None):
            lines.append(f"  Paywalled HTML ({len(paywalled_html)}):")
            for _, row in paywalled_html.iterrows():
                lines.append(f"    - {row['Document Name']}")
        if not pdf_no_filename.empty:
            lines.append(f"  PDF without File Name ({len(pdf_no_filename)}):")
            for _, row in pdf_no_filename.iterrows():
                lines.append(f"    - {row['Document Name']}")
        if orphan_reasons:
            lines.append(f"  Orphan metadata (no matching .md): {len(orphan_reasons)}")
            for name, reason in orphan_reasons:
                lines.append(f"    - {name}: {reason}")
        if missing_metadata:
            lines.append(f"  Missing metadata (no .metadata.json): {len(missing_metadata)}")
            for name in missing_metadata:
                lines.append(f"    - {name}")
        if oversized:
            lines.append(f"  Oversized (>1MB, skipped for S3): {len(oversized)}")
            for path, size in oversized:
                lines.append(f"    - {path.relative_to(md_output_dir)} ({size / 1e6:.1f}MB)")
    else:
        lines.append(f"\nNo gaps found.")

    # --- Write to file ---
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"pipeline_summary_{timestamp}.txt"
    output_path.write_text("\n".join(lines) + "\n")
    print(f"Pipeline summary written to {output_path}")


def extract_year(date_value):
    """Extract a 4-digit year from a spreadsheet date field.

    Handles datetime objects, bare year ints, and messy strings like
    "Fall 2009", "2013-2014", "Unclear".

    Args:
        date_value: Value from the Publication Date column (datetime, int, str, or None)

    Returns:
        Integer year (e.g. 2009), or None if no year can be extracted
    """
    import datetime

    if date_value is None:
        return None
    if isinstance(date_value, datetime.datetime):
        return date_value.year
    # For other formats of publication date, grab the first year mentioned in the entry
    match = re.search(r'\d{4}', str(date_value))
    if match:
        return int(match.group())
    return None
 

def get_s3_bucket_uris(bucket_name: str, prefix: str = "") -> list:
    """
    List all URIs of objects in an S3 bucket with a given prefix.

    Args:
        bucket_name (str): The name of the S3 bucket.
        prefix (str): The prefix to filter objects (optional).

    Returns:
        list: A list of S3 URIs for the objects in the bucket.
    """
    
    s3_client = boto3.client('s3')
    paginator = s3_client.get_paginator('list_objects_v2')
    
    uris = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get('Contents', []):
            uris.append(f"s3://{bucket_name}/{obj['Key']}")
    
    return uris
