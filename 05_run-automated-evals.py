"""
AI Disclaimer: This script was created with GitHub Copilot's assistance.

This script runs the automated evaluation pipeline by:
1. Loading the question to auto eval mapping from a JSON file.
2. Loading the auto eval data from a JSON file.
3. Running the specified auto evals for each question.
4. Saving the results to an output JSON file.

Usage:
    python 05_run-automated-evals.py --auto_eval_file <auto_eval_file_name.json>
    python 05_run-automated-evals.py --auto_eval_file <auto_eval_file_name.json> --tests 5 6
"""

import json
import argparse
import os
import boto3

from utils.eval_metrics import (
    calculate_bert_score,
    calculate_bleurt_score,
    process_promptfoo_similar_tests,
    process_promptfoo_refusal_tests,
    evaluate_retrieval_at_k,
    evaluate_source_link_metrics,
    auto_evals_summary_statistics,
    AUTO_EVAL_MAP
)

from config import REGION, BUCKET_NAME

# Define constants
SIMILARITY_THRESHOLD = 0.8
K_FOR_RETRIEVAL = 5

# Eval function mapping for dynamic calling
AUTO_EVAL_FUNCTIONS = {
    "ae-test-1": lambda eval_data: calculate_bert_score(eval_data.get("response", ""), eval_data.get("reference", "")),
    "ae-test-2": lambda eval_data: calculate_bleurt_score(eval_data.get("response", ""), eval_data.get("reference", "")),
    "ae-test-3": lambda eval_data, auto_eval_id: process_promptfoo_refusal_tests(eval_data, auto_eval_id),
    "ae-test-4": lambda eval_data, auto_eval_id: process_promptfoo_similar_tests(eval_data, threshold=SIMILARITY_THRESHOLD, auto_eval_id=auto_eval_id),
    "ae-test-5": lambda eval_data: evaluate_retrieval_at_k(eval_data, k=K_FOR_RETRIEVAL),
    "ae-test-6": lambda eval_data: evaluate_source_link_metrics(eval_data)
}


def load_auto_eval_data_and_mapping(auto_eval_path: str) -> tuple:
    """
    Load auto eval data from a JSON file and create a mapping of auto eval IDs to their corresponding question IDs.

    Args:
        auto_eval_path (str): Path to the auto eval JSON file.

    Returns:
        tuple: A tuple containing:
            - auto_eval_data (dict): A dictionary of question IDs containing auto eval data for each question.
            - auto_evals_dict (dict): A dictionary mapping auto eval IDs to lists of question IDs that require that eval.
    """
    with open(auto_eval_path, "r") as f:
        data = json.load(f)

    auto_evals_dict = {key: [] for key in AUTO_EVAL_MAP.keys()}

    if not isinstance(data, dict):
        raise ValueError("Auto eval data is not a dictionary — cannot create mapping of question IDs to auto eval IDs")
    else:
        question_ids = data.keys()

    for q_id in question_ids:
        for eval_id in data[q_id].get("auto_evals", []):
            if eval_id in AUTO_EVAL_MAP:
                auto_evals_dict[eval_id].append(q_id)
            else:
                print(f"Warning: unknown auto eval ID {eval_id} for question {q_id} — skipping")

    return data, auto_evals_dict


def run_pipeline(auto_eval_path: str, output_path: str, auto_eval_id: str, selected_tests: list = None) -> list:
    """
    Main pipeline — runs evals grouped by eval_id, records results per question.

    Args:
        auto_eval_path: Path to the auto eval JSON file.
        output_path: Path to write results JSON.
        auto_eval_id: ID for this eval run.
        selected_tests: List of test numbers to run (e.g., [1, 5, 6]). If None, runs all.
    """
    print("Loading auto eval data and mapping...")
    auto_eval_data, auto_evals_dict = load_auto_eval_data_and_mapping(auto_eval_path)

    # Filter to selected tests if specified
    if selected_tests:
        selected_eval_ids = {f"ae-test-{n}" for n in selected_tests}
        auto_evals_dict = {k: v for k, v in auto_evals_dict.items() if k in selected_eval_ids}
        print(f"Running selected tests: {', '.join(sorted(selected_eval_ids))}")

    # Accumulator: {question_id: {eval_id: result, ...}}
    results_by_question = {qid: {"question_id": qid} for qid in auto_eval_data}

    # Run evals grouped by eval_id
    BATCH_EVAL_IDS = {"ae-test-3", "ae-test-4"}

    for eval_id, question_ids in auto_evals_dict.items():
        if not question_ids:
            continue

        eval_type = AUTO_EVAL_MAP.get(eval_id)
        eval_func = AUTO_EVAL_FUNCTIONS.get(eval_id)
        if eval_func is None:
            print(f"  Warning: no function mapped for {eval_id} — skipping")
            continue

        print(f"\n--- Running {eval_id} ({eval_type}) for {len(question_ids)} question(s) ---")

        if eval_id in BATCH_EVAL_IDS:
            # Pass only the subset of questions that need this eval
            batch_input = {qid: auto_eval_data[qid] for qid in question_ids if qid in auto_eval_data}
            try:
                batch_results = eval_func(batch_input, auto_eval_id) # Returns a dict of {question_id: eval_result}
                for qid, value in batch_results.items():
                    results_by_question[qid][eval_id] = value
            except Exception as e:
                print(f"  ERROR {eval_id}: {e}")
                for qid in question_ids:
                    results_by_question[qid][eval_id] = None

        else:
            for question_id in sorted(set(question_ids)):
                output_obj = auto_eval_data.get(question_id)
                if output_obj is None:
                    print(f"  Warning: {question_id} not found — skipping")
                    continue

                try:
                    results_by_question[question_id][eval_id] = eval_func(output_obj)
                except Exception as e:
                    print(f"  ERROR {eval_id} for {question_id}: {e}")
                    results_by_question[question_id][eval_id] = None

        print(f"  Done.")

    all_results = {
            "auto_eval_results": [ results_by_question[qid] for qid in sorted(results_by_question) ],
            "auto_eval_summary_stats": auto_evals_summary_statistics(results_by_question)
        }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print(f"\nResults saved to {output_path}")
    return all_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run automated evaluations pipeline")
    parser.add_argument("--auto_eval_file", type=str, required=True,
                        help="Name of the auto eval JSON file in results/")
    parser.add_argument("--tests", type=int, nargs="+", default=None,
                        help="Test numbers to run (e.g., --tests 5 6). If not specified, runs all tests.")
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=REGION)
    S3_RESULTS_PREFIX = "results/"  # S3 folder prefix

    local_input  = os.path.join("results", args.auto_eval_file)
    local_output = os.path.join("results", args.auto_eval_file.replace(".json", "_results.json"))

    # 1. Download input from S3 if not already local
    if not os.path.exists(local_input):
        s3_key = f"output/{args.auto_eval_file}"
        print(f"Downloading s3://{BUCKET_NAME}/{s3_key} -> {local_input}")
        os.makedirs("results", exist_ok=True)
        s3.download_file(BUCKET_NAME, s3_key, local_input)

    # 2. Run the pipeline
    run_pipeline(
        auto_eval_path=os.path.join("results", args.auto_eval_file),
        output_path=os.path.join("results", args.auto_eval_file.replace(".json", "_results.json")),
        auto_eval_id=args.auto_eval_file.replace(".json", ""),
        selected_tests=args.tests
    )

    # 3. Upload output to S3
    s3_output_key = f"output/{args.auto_eval_file.replace('.json', '_results.json')}"
    s3.upload_file(local_output, BUCKET_NAME, s3_output_key)
    print(f"Uploaded results to s3://{BUCKET_NAME}/{s3_output_key}")