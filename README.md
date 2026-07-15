# umi-ai-navigator

This README covers the end-to-end pipeline for building, testing, and evaluating the UMI Bedrock Knowledge Base. Each numbered script is designed to run in sequence.

---

## Setup

### Python Environment

This project uses [`uv`](https://docs.astral.sh/uv/) for Python virtual environments and package management.

```bash
uv venv
# macOS/Linux: source .venv/bin/activate
# Windows:     .venv\Scripts\activate
uv pip install -r requirements.txt
```

### AWS CLI Access

Configure temporary federated credentials for Bedrock:

```bash
export AWS_ACCESS_KEY_ID=your-access-key
export AWS_SECRET_ACCESS_KEY=your-secret-key
export AWS_SESSION_TOKEN=your-session-token
```

Credentials expire periodically and must be refreshed from the AWS Console access portal.

### Environment Variables (`.env`) - One-time KB Setup Only

**Only needed when initially creating a Knowledge Base.** The exact tags are stored in S3. Download them as your local `.env`:

```bash
aws s3 cp s3://your-eval-bucket/aws_tags.env .env
```

For scripts `00` and `01`, the `.env` must also include:

- `DATA_FOLDER` — local path to your synced Box data-collection folder (e.g. `<your-project>/Data Collection`). Must be locally synced (not cloud-only).
- `EVAL_FOLDER` — local path to the evaluation framework folder on Box (used by `04`).

The `.env` file is gitignored and should never be committed.

### Shared Configuration (`config.py`)

The `config.py` file contains shared configuration used by all scripts:

- **KB_CONFIGS**: Knowledge base type configurations
- **AWS settings**: Region, bucket names, model ARNs
- **Metadata schema**: S3 URI for metadata filtering schema
- **System prompt**: Path to system prompt file

**When to edit config.py:**
- Adding a new KB type (e.g., 'custom-parser')
- Changing AWS region or bucket names
- Updating model ARNs (embedding or generation models)
- Changing metadata schema or system prompt locations

**Collaborators should commit changes to `config.py`** so everyone stays in sync.

---

## Pipeline Scripts

### `00_html_to_markdown.py` — Scrape HTML to Markdown

Converts web pages to clean Markdown files with YAML frontmatter (title, date, description). Reads source URLs and metadata from `Knowledge Base Metadata.xlsx` on Box Drive, generates `urls_config.json`, and scrapes each page using `trafilatura`.

Upward Mobility Framework predictor pages are automatically split into separate evidence and interventions files.

```bash
python 00_html_to_markdown.py
```

**Intermediate output:** `urls_config.json` (consumed by script `01`)

```json
{
  "urls": [
    {
      "url": "https://{policy-organization}.org/policy-report",
      "filename": "policy_report.md",
      "subfolder": "{policy-organization}"
    }
  ]
}
```

**Output:** `data/md/<subfolder>/*.md`  

Each file includes YAML frontmatter:

```yaml
---
title: Policy Report Title
url: https://{policy-organization}.org/policy-report
hostname: {policy-organization}.org
description: Summary of the report...
date: 2024-01-15  # publication date
---
```

### `01_generate-metadata-tags.py` — Generate Metadata and Upload to S3

Produces `.metadata.json` sidecar files for each document. Handles two file types:

- **HTML** — Merges `urls_config.json` (from `00`) with the metadata spreadsheet to tag scraped markdown.
- **PDF** — Locates PDFs on Box Drive, converts to markdown via `docling`, and tags them.

Optionally mirrors the local `data/md/` directory to S3 with `--sync-s3`.

```bash
python 01_generate-metadata-tags.py                # generate metadata only
python 01_generate-metadata-tags.py --sync-s3      # generate metadata + upload to S3
python 01_generate-metadata-tags.py --skip-existing # skip docs that already have metadata
```

#### How `00` and `01` work together

`urls_config.json` is the bridge between the two scripts. `00` generates it by reading source URLs from `Knowledge Base Metadata.xlsx`, scraping each page, deriving filenames from page titles, and recording the URL-to-filename mapping. `01` then merges this config with the spreadsheet to produce `.metadata.json` sidecar files.

**Spreadsheet file types** control the pipeline flow:

| File Type | Pipeline | Notes |
|---|---|---|
| `HTML` | Scraped by `00`, tagged by `01` | Standard web pages |
| `PDF` | Saved manually, converted by `01` (docling), tagged by `01` | Requires `File Name` column in spreadsheet |
| `Interactive` | Excluded | JS-rendered pages that can't be scraped |

**Key files:**
- `config.py` — `PROJECT_ROOT`, `DATA_FOLDER`, `METADATA_PATH` (from Box via `.env`), AWS settings
- `utils/utils.py` — Shared utilities: `slugify()` (filename derivation), `extract_year()` (date parsing)
- `tests/test_data_pipeline.py` — Validates spreadsheet integrity, config completeness, file structure, metadata content

**S3 upload (`--sync-s3`):** Mirrors `data/md/` to S3 — uploads all paired `.md` + `.metadata.json` files, deletes S3 objects that no longer exist locally. Files without a `.metadata.json` are skipped. To keep the corpus current, run `01 --sync-s3` followed by `02 --sync`.

### `02_create-bedrock-kb.py` — Create and Sync Knowledge Base

Creates the AWS Bedrock Knowledge Base (IAM roles, vector store, data source) or syncs new/updated documents from S3.

```bash
# Create a new KB
python 02_create-bedrock-kb.py --setup --kb-type default-md

# Sync documents to an existing KB
python 02_create-bedrock-kb.py --sync --kb-type default-md
```

Available KB types are defined in `config.py` under `KB_CONFIGS` (e.g., `default-md`, `automation-pdf`).

> **Note:** If you change KB configuration (IAM policies, parser, chunking), delete and recreate the KB. For document-only updates, just re-run `--sync`.

### `03_test-bedrock-kb.py` — Test the Knowledge Base

Programmatic testing of retrieval and generation against the KB. Supports both single-query CLI mode and an interactive REPL.

```bash
# Interactive mode (recommended)
python 03_test-bedrock-kb.py --interactive

# Single-query examples
python 03_test-bedrock-kb.py --query "What are living wages?" --cite-urls
python 03_test-bedrock-kb.py --query "..." --compare        # filtered vs. unfiltered
python 03_test-bedrock-kb.py --query "..." --retrieve-only   # chunks only, no generation
```

Key interactive commands: `<query>`, `/retrieve`, `/retrieve-nofilter`, `/compare`, `/nofilter`, `/cite`. See the script's docstring or the interactive help for the full list and cost indicators.

Available commands with cost indicators:
- `<query>` - Retrieve and generate response [💰💰 Filter + Generation]
- `/retrieve <query>` - Retrieve only, with filtering [💰 Filter only]
- `/retrieve-nofilter <q>` - Retrieve only, no filtering [💲 Cheapest - retrieval only]
- `/compare <query>` - Compare filtered vs unfiltered retrieval [💰💰 2x Retrieval + Filter]
- `/nofilter <query>` - Query without metadata filtering [💰 Generation only]
- `/cite <query>` - Generate with URL citations [💰💰 Filter + Generation]

#### How the script works

1. **Configuration:** Imports model ARNs, credentials, and paths from `config.py`. Initializes the Bedrock Agent Runtime client and loads the metadata schema from S3 for filtering.
2. **`build_retrieval_config()`** determines the value of `k` and whether to apply manual, LLM-based, or no metadata filtering. Called by both retrieval and generation functions.
3. **`get_knowledge_base_id()`** resolves the KB name to its current AWS ID (the ID changes when a KB is deleted and recreated).

**Core functions (in order of complexity):**

| Function | Use case |
|---|---|
| `retrieve_chunks()` | Return top-k chunks only — no generation. Use for inspecting retrieval quality. |
| `retrieve_and_generate()` | Retrieve + generate a response via Bedrock's single-call API. Simple diagnostic; no custom system prompt. |
| `retrieve_and_generate_with_urls()` | **Preferred for final output.** Separates retrieval and generation so URLs are appended to context before prompting the model. Allows the custom system prompt and full control over the prompt. |

Display helpers live in `utils/kb_display.py` (`print_retrieval_results()`, `print_generation_results()`, etc.).

### `04_generate-responses-for-eval.py` — Generate Evaluation Responses

Runs evaluation prompts from `full-evaluation-methodology.xlsx` (on Box) against the KB and writes two output files:

- `results/human_eval.csv` — prompt + response for human review
- `results/auto_eval.json` — full detail (chunks, scores, metadata) for automated eval

```bash
python 04_generate-responses-for-eval.py                          # all prompts
python 04_generate-responses-for-eval.py --question-ids t1-q1     # specific question
python 04_generate-responses-for-eval.py --task-category "Policy"  # by category
python 04_generate-responses-for-eval.py --n-responses 3           # multiple responses per prompt
```

### `05_run-automated-evals.py` — Run Automated Evaluations

Runs automated evaluation tests against the generated responses from `04`. Available tests include BERTScore, BLEURT, promptfoo similarity/refusal detection, retrieval-at-k, and source link metrics.

```bash
python 05_run-automated-evals.py --auto_eval_file <auto_eval_file.json>
python 05_run-automated-evals.py --auto_eval_file <auto_eval_file.json> --tests 5 6
```

**Additional dependency:** Tests 3 and 4 require [promptfoo](https://www.promptfoo.dev/) (Node.js CLI) and a Hugging Face API token.

#### Promptfoo setup

**Prerequisites:**
- [Node.js](https://nodejs.org/) 18+ (LTS recommended). Verify with `node --version` and `npm --version`.
- A [Hugging Face](https://huggingface.co/welcome) account with a [read-access token](https://huggingface.co/docs/hub/en/security-tokens) (required for the embedding model in test 4).

**Install promptfoo globally:**

```bash
npm install -g promptfoo@latest
promptfoo --version   # verify
```

**Set the Hugging Face token** (read by `utils/promptfoo/promptfooconfig.yaml`):

```powershell
# PowerShell
$env:HF_TOKEN = "hf_..."
```

```bash
# bash / zsh
export HF_TOKEN="hf_..."
```

---

## Testing

Validate the data pipeline (scripts `00` and `01`) with the included test suite:

```bash
python -m pytest tests/test_data_pipeline.py -v
```

Tests cover spreadsheet integrity, `urls_config.json` completeness, file structure, metadata content, and count reconciliation. Run from the project root (`pyproject.toml` adds the root to `pythonpath`).

---

## Project Structure (Pipeline-Relevant)

```
├── 00_html_to_markdown.py
├── 01_generate-metadata-tags.py
├── 02_create-bedrock-kb.py
├── 03_test-bedrock-kb.py
├── 04_generate-responses-for-eval.py
├── 05_run-automated-evals.py
├── config.py                   # Shared configuration
├── requirements.txt            # Python dependencies
├── utils/                      # Shared utilities (eval_metrics, kb_display, promptfoo config)
├── tests/                      # Pipeline validation tests
├── prompts/                    # System prompt files
```