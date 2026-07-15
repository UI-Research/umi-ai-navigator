"""
Runs a set of prompts from the evaluation framework Excel on Box against the
Bedrock Knowledge Base and writes two output files for evaluation, both locally
and to S3:

  results/human_eval.csv   — prompt + response for human review in Shiny
  results/auto_eval.json   — full detail (chunks, scores, metadata) for automated eval

Usage:
    python 04_generate-responses-for-eval.py
    python 04_generate-responses-for-eval.py --question-ids t1-q1
    python 04_generate-responses-for-eval.py --task-category "Policy"
    python 04_generate-responses-for-eval.py --n-responses 3
"""

import boto3
import json
import csv
import argparse
import os
from datetime import datetime
from typing import Optional
from utils.utils import get_s3_bucket_uris

import pandas as pd

from config import (
    KB_CONFIGS,
    REGION,
    BUCKET_NAME,
    SHINY_BUCKET_NAME,
    METADATA_SCHEMA_S3_URI,
    SYSTEM_PROMPT_PATH,
    GENERATION_MODEL_ARN_LIGHT,
    GENERATION_MODEL_ARN,
    FILTER_GENERATION_MODEL_ARN,
    EVAL_PROMPTS_PATH
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
OUTPUT_DIR = "results"

# ---------------------------------------------------------------------------
# Column names in the evaluation framework Excel
# ---------------------------------------------------------------------------
COL_QUESTION_ID    = "Question ID"
COL_TASK_CATEGORY  = "Task Category"
COL_INFO_CATEGORY  = "Information Category"
COL_QUESTION       = "Question"
COL_REFERENCE_DOC_ID = "Document ID"
COL_AUTO_EVALS     = "Auto Evals"
COL_GROUND_TRUTH   = "Ground Truth Response"

EVAL_SHEET_NAME = "Evaluation Matrix Full"


def load_prompts(excel_path=EVAL_PROMPTS_PATH) -> list[dict]:
    """
    Load prompts from the evaluation framework Excel on Box.

    Args:
        excel_path: Path to the Excel file. Reads the 'Evaluation Matrix Full'
                    sheet. Expected columns: Question ID, Task Category,
                    Information Category, Question.

    Returns:
        List of dicts, one per row, keyed by column name.
    """
    df = pd.read_excel(excel_path, sheet_name=EVAL_SHEET_NAME)
    df = df[df[COL_QUESTION_ID].notna() & (df[COL_QUESTION_ID].str.strip() != "")]
    return df.to_dict(orient="records")


def filter_prompts(
    prompts: list[dict],
    question_ids: Optional[list[str]] = None,
    task_category: Optional[str] = None,
    info_category: Optional[str] = None
) -> list[dict]:
    """
    Return only the prompts that match the supplied filters.
    All filters are optional; if none are provided, all prompts are returned.
    Multiple filters are combined with AND logic.

    Args:
        prompts:      List of prompt row dicts, as returned by load_prompts().
        question_ids: If provided, keep only rows whose Question ID is in this list.
        task_category: If provided, keep only rows whose Task Category exactly matches.
        info_category: If provided, keep only rows whose Information Category exactly matches.

    Returns:
        Filtered list of prompt row dicts.
    """
    if question_ids:
        prompts = [p for p in prompts if p[COL_QUESTION_ID] in question_ids]
    if task_category:
        prompts = [p for p in prompts if p[COL_TASK_CATEGORY] == task_category]
    if info_category:
        prompts = [p for p in prompts if p[COL_INFO_CATEGORY] == info_category]
    return prompts


def build_auto_eval_record(row: dict, response: dict, run_index: int) -> dict:
    """
    Build a single auto-eval record from a prompt row and a cite-urls API response.

    Args:
        row:       A single prompt row dict from load_prompts(), containing the
                   four CSV columns (Question ID, Task Category, etc.).
        response:  Return value of retrieve_and_generate_with_urls(). Contains:
                     generated_text     — the model's full response string
                     sources            — list of {index, url, score, metadata},
                                          one per retrieved chunk (no chunk text)
                     retrieval_response — raw Bedrock retrieve() response; chunk
                                          text lives here under retrievalResults
        run_index: 1-based counter for which response this is when n_responses > 1.
                   Always 1 for single-response runs.

    Returns:
        Dict with prompt metadata, the generated response, and a parsed 'chunks'
        list — each chunk has text, url, s3_uri, score, and filtered metadata.
    """
    # Parse retrieval results into clean chunk records
    raw_results = response['retrieval_response'].get('retrievalResults', [])
    chunks = []
    for result in raw_results:
        metadata = result.get('metadata', {})
        s3_uri   = result.get('location', {}).get('s3Location', {}).get('uri', '')
        chunks.append({
            'text':     result.get('content', {}).get('text', ''),
            'url':      metadata.get('url', s3_uri),
            's3_uri':   s3_uri,
            'score':    result.get('score'),
            'metadata': {k: v for k, v in metadata.items()
                         if not k.startswith('AMAZON_BEDROCK')}
        })

    # Validate that all S3 URIs in the reference document IDs column are valid objects in the bucket associated with the KB
    row_col_ref_docs = row.get(COL_REFERENCE_DOC_ID, "")
    if isinstance(row_col_ref_docs, str) and row_col_ref_docs.strip():
        reference_uris = set(d.strip() for d in row_col_ref_docs.split(","))
        all_valid_uris = set(get_s3_bucket_uris(BUCKET_NAME, "documents-md"))
        if not reference_uris.issubset(all_valid_uris):
            print(f"WARNING: Not all reference doc URIs are valid S3 objects: {reference_uris - all_valid_uris}")
    else:
        print("No reference doc IDs provided.")
        reference_uris = None
    
    # Get ground truth response if available; otherwise set to None 
    ground_truth = row.get(COL_GROUND_TRUTH, "") or None
    
    return {
        'question_id':   row[COL_QUESTION_ID],
        'task_category': row[COL_TASK_CATEGORY],
        'info_category': row[COL_INFO_CATEGORY],
        'question':      row[COL_QUESTION],
        'run_index':     run_index,
        'response':      response['generated_text'],
        'chunks':        chunks,
        'auto_evals':    [e.strip() for e in row.get(COL_AUTO_EVALS, "").split(", ")] if isinstance(row.get(COL_AUTO_EVALS), str) and row[COL_AUTO_EVALS].strip() else [],
        'relevant_docs': list(reference_uris) if reference_uris is not None else [],
        'reference': ground_truth,
        # start with the first response; if multiple runs, additional responses will be appended to this list
        'n_responses':   [response['generated_text']]  
    }


def write_human_eval(records: list[dict], path: str):
    """
    Write human eval output to a CSV file.

    Args:
        records: List of dicts built in the eval loop. Each has: Question ID,
                 Task Category, Information Category, Question, run_index,
                 response (the model's text), and error (None if successful).
        path:    Destination file path (created or overwritten).
    """
    fieldnames = [
        COL_QUESTION_ID, COL_TASK_CATEGORY, COL_INFO_CATEGORY,
        COL_QUESTION, 'run_index', 'generation_model', 'filter_status',
        'response', 'error'
    ]
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def write_auto_eval(records: list[dict], path: str):
    """
    Write automated eval output to a JSON file.

    Args:
        records: List of dicts from build_auto_eval_record(). Each has prompt
                 metadata, the generated response, and a 'chunks' list with
                 text, url, score, and metadata per retrieved chunk.
        path:    Destination file path (created or overwritten).
    """
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(records, f, indent=2, default=str)


def write_reviewer_excel(auto_records, path: str):
    """
    Write a two-sheet Excel workbook for reviewer fact-checking.

    Sheet 1 ("Responses"): one row per question/run with the generated response.
    Sheet 2 ("Citations"): one row per retrieved chunk, linked back to the
    response by question_id + run_index.

    Args:
        auto_records: Dict or list of dicts from build_auto_eval_record(), each
                      containing prompt metadata, response text, and chunks.
        path:         Destination .xlsx file path.
    """
    from openpyxl import Workbook

    # Convert dict to list of values if needed
    records_list = list(auto_records.values()) if isinstance(auto_records, dict) else auto_records

    wb = Workbook()

    # --- Responses sheet ---
    ws_resp = wb.active
    ws_resp.title = "Responses"
    resp_headers = [
        "question_id", "run_index", "task_category",
        "info_category", "question", "response"
    ]
    ws_resp.append(resp_headers)
    for rec in records_list:
        ws_resp.append([
            rec.get('question_id'), rec.get('run_index'),
            rec.get('task_category'), rec.get('info_category'),
            rec.get('question'), rec.get('response')
        ])

    # --- Citations sheet ---
    ws_cite = wb.create_sheet("Citations")
    cite_headers = [
        "question_id", "run_index", "chunk_index",
        "score", "document_name", "source", "url", "chunk_text"
    ]
    ws_cite.append(cite_headers)
    for rec in records_list:
        for i, chunk in enumerate(rec.get('chunks', []), 1):
            meta = chunk.get('metadata', {})
            ws_cite.append([
                rec.get('question_id'),
                rec.get('run_index'),
                i,
                chunk.get('score'),
                meta.get('document_name', ''),
                meta.get('source', ''),
                chunk.get('url', ''),
                chunk.get('text', '')
            ])

    wb.save(path)
    print(f"Reviewer Excel written to {path}")


def build_output_paths() -> tuple[str, str, str, str, str]:
    """
    Create the output directory and return local and S3 file paths.

    Returns:
        Tuple of (local_human_path, local_auto_path, local_reviewer_path,
        s3_human_path, s3_auto_path).
        Filenames include a timestamp so repeated runs don't overwrite each other.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_human    = os.path.join(OUTPUT_DIR, f"human_eval_{timestamp}.csv")
    local_auto     = os.path.join(OUTPUT_DIR, f"auto_eval_{timestamp}.json")
    local_reviewer = os.path.join(OUTPUT_DIR, f"reviewer_{timestamp}.xlsx")
    s3_human       = f"human_eval_{timestamp}.csv"
    s3_auto        = f"output/auto_eval_{timestamp}.json"
    return local_human, local_auto, local_reviewer, s3_human, s3_auto


def main():
    parser = argparse.ArgumentParser(
        description="Generate KB responses for a set of prompts and write eval output files."
    )
    parser.add_argument(
        '--question-ids',
        nargs='+',
        metavar='ID',
        help='Run only these Question IDs (e.g. --question-ids Q1 Q3 Q10)'
    )
    parser.add_argument(
        '--task-category',
        help='Filter prompts by Task Category column value'
    )
    parser.add_argument(
        '--info-category',
        help='Filter prompts by Information Category column value'
    )
    parser.add_argument(
        '--n-responses',
        type=int,
        default=1,
        metavar='N',
        help='Number of responses to generate per prompt (default: 1)'
    )
    parser.add_argument(
        '--kb-type',
        choices=list(KB_CONFIGS.keys()),
        default='default-md',
        help='Knowledge base type (default: default-md)'
    )
    parser.add_argument(
        '--kb-id',
        help='Knowledge Base ID (overrides --kb-type lookup)'
    )
    parser.add_argument(
        '--no-filter',
        action='store_true',
        help='Disable implicit metadata filtering during retrieval'
    )
    args = parser.parse_args()

    # Resolve KB ID
    if args.kb_id:
        kb_id = args.kb_id
    else:
        kb_name = KB_CONFIGS[args.kb_type]['name']
        print(f"Looking up Knowledge Base: {kb_name}")
        kb_id = get_knowledge_base_id(kb_name)
        if not kb_id:
            print(f"Error: Knowledge Base '{kb_name}' not found")
            return 1

    print(f"Using Knowledge Base ID: {kb_id}")

    # Load + filter prompts
    all_prompts = load_prompts()
    prompts = filter_prompts(
        all_prompts,
        question_ids=args.question_ids,
        task_category=args.task_category,
        info_category=args.info_category
    )
    print(f"Running {len(prompts)} prompt(s), {args.n_responses} response(s) each.")

    # Prepare output paths
    local_human, local_auto, local_reviewer, s3_human_key, s3_auto_key = build_output_paths()
    print(f"Human eval  → s3://{SHINY_BUCKET_NAME}/{s3_human_key}")
    print(f"Auto eval   → s3://{BUCKET_NAME}/{s3_auto_key}")

    # Core eval loop
    human_records = []
    auto_records  = {}

    for i, row in enumerate(prompts, 1):
        question_id = row[COL_QUESTION_ID]
        question    = row[COL_QUESTION]
        # Route simpler question types to Haiku and more complex ones to Sonnet, to balance cost and quality.
        if row[COL_TASK_CATEGORY] in ['Extractive QA', 'Single-hop reasoning']:
            generation_model = GENERATION_MODEL_ARN_LIGHT
        else:
            generation_model = GENERATION_MODEL_ARN
        print(f"\n[{i}/{len(prompts)}] {question_id}: {question[:70]}...")

        for run in range(1, args.n_responses + 1):
            if args.n_responses > 1:
                print(f"  Run {run}/{args.n_responses}")
            try:
                response = retrieve_and_generate_with_urls(
                    kb_id, question,
                    use_metadata_filter=not args.no_filter,
                    generation_model_arn=generation_model,
                )
                filter_status = response.get('filter_status', 'unknown')
                print(f"  Filter status: {filter_status}")

                if run == 1: 
                    human_records.append({
                        COL_QUESTION_ID:   question_id,
                        COL_TASK_CATEGORY: row[COL_TASK_CATEGORY],
                        COL_INFO_CATEGORY: row[COL_INFO_CATEGORY],
                        COL_QUESTION:      question,
                        'run_index':       run,
                        'generation_model': generation_model,
                        'filter_status':   filter_status,
                        'response':        response['generated_text'],
                        'error':           None
                    })
                    auto_records[question_id] = build_auto_eval_record(row, response, run)
                    auto_records[question_id]['generation_model'] = generation_model
                    auto_records[question_id]['filter_status'] = filter_status
                else:
                    # Append additional responses to the existing record for this question ID
                    if question_id in auto_records:
                        auto_records[question_id]['n_responses'].append(response['generated_text'])

            except Exception as e:
                print(f"  ERROR: {e}")
                error_stub = {
                    COL_QUESTION_ID:   question_id,
                    COL_TASK_CATEGORY: row[COL_TASK_CATEGORY],
                    COL_INFO_CATEGORY: row[COL_INFO_CATEGORY],
                    COL_QUESTION:      question,
                    'run_index':       run,
                    'generation_model': generation_model,
                    'filter_status':   'error',
                    'response':        "",
                    'error':           str(e)
                }
                human_records.append(error_stub)
                auto_records[question_id] = (error_stub)

    # Write locally then upload to S3
    write_human_eval(human_records, local_human)
    write_auto_eval(auto_records, local_auto)
    write_reviewer_excel(auto_records, local_reviewer)

    s3.upload_file(local_human, SHINY_BUCKET_NAME, s3_human_key)
    s3.upload_file(local_auto, BUCKET_NAME, s3_auto_key)

    success = sum(1 for r in human_records if r['error'] is None)
    print(f"\nDone. {success}/{len(human_records)} successful.")
    print(f"Local:  {local_human}, {local_auto}, {local_reviewer}")
    print(f"S3:     s3://{SHINY_BUCKET_NAME}/{s3_human_key}, s3://{BUCKET_NAME}/{s3_auto_key}")
    return 0


if __name__ == "__main__":
    # Re-use retrieval logic from the test script
    from importlib import import_module
    test_script = import_module("03_test-bedrock-kb")
    s3 = boto3.client('s3', region_name=REGION)
    
    retrieve_and_generate_with_urls = test_script.retrieve_and_generate_with_urls
    get_knowledge_base_id = test_script.get_knowledge_base_id
    exit(main())
