"""
Validate the data collection and tagging pipeline (scripts 00 and 01).

Covers:
- Spreadsheet integrity (input validation)
- urls_config.json (00's output)
- Metadata files and markdown pairing (01's output)
- Count reconciliation against the spreadsheet

Usage: python -m pytest tests/test_data_pipeline.py -v
"""

import json
import re

import pandas as pd
import pytest

from config import PROJECT_ROOT, METADATA_PATH

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
MD_OUTPUT_DIR = PROJECT_ROOT/"data"/"md"
URLS_CONFIG_PATH = PROJECT_ROOT/"urls_config.json"
SCHEMA_PATH = PROJECT_ROOT/"data"/"md"/"metadata_schema.json"

VALID_SUBFOLDERS = {"urban_mobility", "other_academic"}
REQUIRED_KEYS = {"document_name", "url", "source", "document_type", "pillar_1", "predictor_1"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# Note: The fixture decorator ensures that any function with the parameter `md_files` etc. will automatically receive the return value of `md_files`
@pytest.fixture(scope="module")
def md_files():
    return sorted(MD_OUTPUT_DIR.rglob("*.md"))


@pytest.fixture(scope="module")
def metadata_files():
    return sorted(MD_OUTPUT_DIR.rglob("*.metadata.json"))


@pytest.fixture(scope="module")
def spreadsheet():
    return pd.read_excel(METADATA_PATH)


@pytest.fixture(scope="module")
def urls_config():
    with open(URLS_CONFIG_PATH) as f:
        return json.load(f)["urls"]


# ===========================================================================
# Spreadsheet integrity (input validation)
# ===========================================================================
class TestSpreadsheetIntegrity:
    """Validate the metadata spreadsheet before the pipeline runs."""

    def test_required_columns_exist(self, spreadsheet):
        required = {"Document Name", "Document URL", "Information Source",
                     "Information Type", "Publication Date", "File Type",
                     "Pillar", "Predictor"}
        missing = required - set(spreadsheet.columns)
        assert not missing, f"Spreadsheet missing columns: {missing}"

    def test_file_type_values(self, spreadsheet):
        valid = {"HTML", "PDF", "Interactive", "Other"}
        actual = set(spreadsheet["File Type"].dropna().unique())
        bad = actual - valid
        assert not bad, f"Unexpected File Type values: {bad}"

    def test_no_empty_document_urls(self, spreadsheet):
        empty = spreadsheet[spreadsheet["Document URL"].isna()]
        assert len(empty) == 0, f"{len(empty)} rows have empty Document URL"

    def test_no_duplicate_file_names_per_url(self, spreadsheet):
        pdf = spreadsheet[spreadsheet["File Type"] == "PDF"]
        pdf_with_files = pdf[pdf["File Name"].notna()]
        dupes = (pdf_with_files.groupby("Document URL")["File Name"]
                 .apply(lambda x: list(x.unique())))
        multi = dupes[dupes.apply(len) > 1]
        failures = [f"{url}: {names}" for url, names in multi.items()]
        assert not failures, (
            f"{len(failures)} URLs with multiple File Names:\n"
            + "\n".join(failures)
        )

    def test_no_conflicting_document_names(self, spreadsheet):
        """Same URL should not map to different Document Names."""
        dupes = (spreadsheet.groupby("Document URL")["Document Name"]
                 .apply(lambda x: list(x.unique())))
        multi = dupes[dupes.apply(len) > 1]
        failures = [f"{url}: {names}" for url, names in multi.items()]
        assert not failures, (
            f"{len(failures)} URLs with conflicting Document Names:\n"
            + "\n".join(failures)
        )


# ===========================================================================
# urls_config.json (00's output)
# ===========================================================================
class TestUrlsConfig:
    """Validate urls_config.json from the scraping step."""

    def test_all_entries_have_subfolder(self, urls_config):
        missing = [e["url"] for e in urls_config if not e.get("subfolder")]
        assert not missing, f"{len(missing)} entries missing subfolder"

    def test_subfolders_are_valid(self, urls_config):
        bad = [(e["url"], e["subfolder"]) for e in urls_config
               if e.get("subfolder") not in VALID_SUBFOLDERS]
        assert not bad, f"Invalid subfolders: {bad}"

    def test_all_entries_have_filename(self, urls_config):
        missing = [e["url"] for e in urls_config if not e.get("filename")]
        assert not missing, (
            f"{len(missing)} URLs not scraped (no filename):\n"
            + "\n".join(missing)
        )

    def test_all_filenames_exist_on_disk(self, urls_config):
        missing = []
        for entry in urls_config:
            if not entry.get("filename"):
                continue
            md_path = MD_OUTPUT_DIR/entry["subfolder"]/entry["filename"]
            if not md_path.exists():
                missing.append(f"{entry['subfolder']}/{entry['filename']}")
        assert not missing, (
            f"{len(missing)} filenames in config but not on disk:\n"
            + "\n".join(missing)
        )


# ===========================================================================
# File structure (01's output)
# ===========================================================================
class TestFileStructure:
    """Every .md should have a .metadata.json and vice versa."""

    def test_every_md_has_metadata(self, md_files):
        missing = []
        for md in md_files:
            meta = md.parent / f"{md.name}.metadata.json"
            if not meta.exists():
                missing.append(str(md.relative_to(MD_OUTPUT_DIR)))
        assert not missing, f"{len(missing)} .md files missing .metadata.json:\n" + "\n".join(missing)

    def test_every_metadata_has_md(self, metadata_files):
        orphans = []
        for meta in metadata_files:
            # metadata_schema.json is not a document metadata file
            if meta.name == "metadata_schema.json":
                continue
            md_name = meta.name.replace(".metadata.json", "")
            md = meta.parent / md_name
            if not md.exists():
                orphans.append(str(meta.relative_to(MD_OUTPUT_DIR)))
        assert not orphans, f"{len(orphans)} .metadata.json files with no matching .md:\n" + "\n".join(orphans)

    def test_files_in_valid_subfolders(self, md_files):
        bad = []
        for md in md_files:
            subfolder = md.parent.name
            if subfolder not in VALID_SUBFOLDERS:
                bad.append(str(md.relative_to(MD_OUTPUT_DIR)))
        assert not bad, f"Files outside valid subfolders:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# Filename checks
# ---------------------------------------------------------------------------
class TestFilenameConventions:
    """All filenames should be slugified: lowercase, underscores only."""

    def test_md_filenames_are_slugified(self, md_files):
        bad = []
        for md in md_files:
            name = md.stem  # filename without .md
            if name != name.lower() or " " in name:
                bad.append(md.name)
        assert not bad, f"{len(bad)} filenames not properly slugified:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# Metadata content checks
# ---------------------------------------------------------------------------
class TestMetadataContent:
    """Validate the contents of each .metadata.json file."""

    def test_required_keys_present(self, metadata_files):
        failures = []
        for meta in metadata_files:
            if meta.name == "metadata_schema.json":
                continue
            data = json.loads(meta.read_text())
            attrs = data.get("metadataAttributes", {})
            missing = REQUIRED_KEYS - set(attrs.keys())
            if missing:
                failures.append(f"{meta.name}: missing {missing}")
        assert not failures, f"{len(failures)} files missing required keys:\n" + "\n".join(failures)

    def test_publication_year_format(self, metadata_files):
        bad = []
        for meta in metadata_files:
            if meta.name == "metadata_schema.json":
                continue
            attrs = json.loads(meta.read_text()).get("metadataAttributes", {})
            year = attrs.get("publication_year")
            if year is not None:
                if not isinstance(year, int) or year < 1900 or year > 2100:
                    bad.append(f"{meta.name}: publication_year={year}")
        assert not bad, f"Invalid publication_year values:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# Count reconciliation against spreadsheet
# ------------------- -------------------------------------------------------
class TestCountReconciliation:
    """Output file counts should match the metadata spreadsheet."""

    def test_total_metadata_matches_spreadsheet(self, metadata_files, spreadsheet, urls_config):
        config_df = pd.DataFrame(urls_config)
        config_urls = set(config_df[config_df["filename"].notna()]["url"].unique())

        html_df = spreadsheet[spreadsheet["File Type"] == "HTML"]
        html_expected = html_df[html_df["Document URL"].isin(config_urls)]["Document URL"].nunique()

        pdf_df = spreadsheet[spreadsheet["File Type"] == "PDF"]
        pdf_expected = pdf_df[pdf_df["File Name"].notna()]["Document URL"].nunique()

        total_expected = html_expected + pdf_expected
        actual = len([m for m in metadata_files if m.name != "metadata_schema.json"])

        assert actual == total_expected, (
            f"Expected {total_expected} metadata files "
            f"({html_expected} HTML + {pdf_expected} PDF), found {actual}"
        )