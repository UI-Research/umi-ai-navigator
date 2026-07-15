"""
This module contains utility functions for automated evaluation metrics.

"""

import numpy as np
from nltk.tokenize import sent_tokenize
from bert_score import BERTScorer
from bleurt import score as bleurt_score
import re
import os
import requests
import zipfile
import pandas as pd
import subprocess
import sys
import itertools
import json
import requests
from nltk.metrics import agreement

# Auto eval ID to function mapping
AUTO_EVAL_MAP = {
    "ae-test-1": "bert_score",
    "ae-test-2": "bleurt_score",
    "ae-test-3": "refusal",
    "ae-test-4": "similarity",
    "ae-test-5": "retrieval",
    "ae-test-6": "source_links"
}

############# BERTScore #############

_BERT_SCORER = None

def get_bert_scorer() -> BERTScorer:
    """Lazily initialize and cache the BERTScorer used by automated evals."""
    global _BERT_SCORER

    if _BERT_SCORER is None:
            _BERT_SCORER = BERTScorer(
                lang="en",
                rescale_with_baseline=True,
                model_type="roberta-large",
            )

    return _BERT_SCORER


def calculate_bert_score(generated_text: str, reference: str):
    """
    Calculate BERTScore for the given generated text and references.

    Code Reference:
      https://onlinelibrary.wiley.com/doi/10.1111/exsy.70003
      https://github.com/Tiiiger/bert_score/blob/master/example/Demo.ipynb

    Args:
        generated_text (str): The generated text to be evaluated.
        reference (str): The reference text for comparison.

    Returns:
        tuple: A tuple containing the mean precision, mean recall, and mean F1 score.
    """

    # Tokenize the generated text and reference into sentences
    generated_sentences = sent_tokenize(generated_text)
    reference_sentences = sent_tokenize(reference)
    scorer = get_bert_scorer()

    # List to store best scores
    best_f1 = []
    best_recall = []
    best_precision = []

    # Iterate over each generated sentence and calculate BERTScore against all reference sentences
    for _, sentence in enumerate(generated_sentences):

        # Compare generated sentence against all reference sentences
        P, R, F1 = scorer.score(
            [sentence] * len(reference_sentences),
            reference_sentences,
        )

        # Append the best scores for the current generated sentence
        best_precision.append(P.max().item())
        best_recall.append(R.max().item())
        best_f1.append(F1.max().item())

    # Return the mean of the best scores across all generated sentences
    mean_precision = np.mean(best_precision)
    mean_recall = np.mean(best_recall)
    mean_f1 = np.mean(best_f1)

    return mean_precision, mean_recall, mean_f1


############# BLEURTScore #############

def setup_bleurt_checkpoint():
    """
    Setup BLEURT-20 checkpoint by downloading the checkpoint file if it doesn't exist.
    Function created based on the instructions from the BLEURT repository and Claude Sonnet 4.5 via GitHub Copilot.

    """

    checkpoint_name = "BLEURT-20"

    # Create output directory if it doesn't exist
    output_dir = "bleurt_checkpoint"
    os.makedirs(output_dir, exist_ok=True)

    # Construct the checkpoint URL and paths
    checkpoint_url = (
        f"https://storage.googleapis.com/bleurt-oss-21/{checkpoint_name}.zip"
    )
    checkpoint_zip_path = os.path.join(output_dir, f"{checkpoint_name}.zip")
    final_path = os.path.join(output_dir, checkpoint_name)

    # Check if the checkpoint already exists
    if not os.path.exists(final_path):

        # Download the checkpoint file with progress indication
        print(f"Downloading BLEURT checkpoint from {checkpoint_url}...")
        response = requests.get(checkpoint_url, stream=True)
        response.raise_for_status()

        # Get total file size for progress tracking
        total = int(response.headers.get("content-length", 0))
        downloaded = 0

        # Write the downloaded content to a file in chunks and show progress
        with open(checkpoint_zip_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                f.write(chunk)
                downloaded += len(chunk)
                # Simple progress: show every 100MB
                if downloaded % (100 * 1024 * 1024) == 0 or downloaded == total:
                    mb_done = downloaded // (1024 * 1024)
                    mb_total = total // (1024 * 1024)
                    print(f"   {mb_done}MB / {mb_total}MB")

        # Extract the checkpoint if it was downloaded
        with zipfile.ZipFile(checkpoint_zip_path, "r") as z:
            z.extractall(output_dir)

        # Cleanup
        os.remove(checkpoint_zip_path)
        print(f"{checkpoint_name} ready at {final_path}")


def calculate_bleurt_score(generated_text: str, reference: str) -> float:
    """
    Calculate BLEURT score for the given generated text and references.

    Code Reference:
    https://github.com/google-research/bleurt?tab=readme-ov-file

    Args:
        generated_text (str): The generated text to be evaluated.
        reference (str): The reference text for comparison.

    Returns:
        list: A list containing the BLEURT scores for each candidate-reference pair.
    """

    # Call the setup function to ensure the BLEURT checkpoint is available
    setup_bleurt_checkpoint()

    # Define the checkpoint path for BLEURT-20
    checkpoint = "bleurt_checkpoint/BLEURT-20"

    # Initialize the BLEURT scorer with the specified checkpoint and calculate the scores
    scorer = bleurt_score.BleurtScorer(checkpoint)
    scores = scorer.score(references=[reference], candidates=[generated_text])

    return scores[0]


############# COSINE SIMILARITY #############

def define_promptfoo_pairwise_similar_tests(
    auto_eval_data: dict, threshold: float, dir_path: str, auto_eval_id: str
) -> str:
    """
    Define pairwise similarity tests for a list of generated responses using `promptfoo` test case format.

    Args:
        auto_eval_data (dict): A dictionary containing auto eval data for each question. Each question ID should map 
        to a record that includes a list of generated responses under the key 'n_responses'.
        threshold (float): The similarity threshold for the tests.
        dir_path (str): The directory path where the test file will be saved.
        auto_eval_id (str): The ID of the auto eval being processed.
    Returns:
        str: The file path of the generated test file.
    """

    tests = []
    for question_id, auto_eval_record in auto_eval_data.items():
        
        generated_responses = auto_eval_record.get("n_responses", [])

        # Get all unique pairs of generated responses for pairwise comparison
        pairs = list(itertools.combinations(generated_responses, 2))

        for i, pair in enumerate(pairs):

            test_case = {
                "description": f"qid={question_id}|pair={i}",
                "vars": {"input": pair[0]},
                "assert": [{"type": "similar", "value": pair[1], "threshold": threshold}],
            }

            tests.append(test_case)

    # Save the generated tests to a JSON file
    test_file_path = f"{dir_path}/similar_tests_{auto_eval_id}.json"

    with open(test_file_path, "w") as json_file:
        json.dump(tests, json_file, indent=4)

    return test_file_path


def process_promptfoo_similar_tests(
    auto_eval_data: dict, threshold: float, auto_eval_id: str
) -> dict:
    """
    Process pairwise similarity tests for a list of generated responses using `promptfoo` and calculate the average similarity score.

    Args:
        auto_eval_data (dict): A dictionary containing auto eval data for each question. 
        threshold (float): The similarity threshold for the tests.
        auto_eval_id (str): The ID of the auto eval being processed.

    Returns:
        dict: A dictionary mapping question IDs to their average similarity score across all pairs of generated responses.
    """

    if not isinstance(auto_eval_data, dict):
        raise ValueError(
            "`auto_eval_data` must be a dictionary containing auto eval data for each question."

        )

    config_path, test_dir_path, output_path = get_promptfoo_directories()
    results_path = os.path.join(output_path, f"similar_tests_results_{auto_eval_id}.csv")

    # Create test file based on generated responses
    test_file_path = define_promptfoo_pairwise_similar_tests(
        auto_eval_data, threshold, test_dir_path, auto_eval_id
    )

    # Run promptfoo evals
    result = run_promptfoo_tests(config_path, test_file_path, results_path)

    # Only raise on actual execution errors, not test failures
    if not os.path.exists(results_path):
        raise RuntimeError(
            f"promptfoo eval failed — no results file created:\n{result.stderr}"
        )

    # Parse results and calculate average scores
    df_scores = pd.read_csv(results_path)

    df_scores["question_id"] = df_scores["Description"].apply(lambda x: x.split("|")[0].split("=")[1])
    
    avg_scores = df_scores.groupby("question_id")["Score"].mean().to_dict()
    print(f"Similar tests complete — {len(avg_scores)} question(s), avg score: {np.mean(list(avg_scores.values())):.3f}")

    # Fill 0.0 for any question with no results
    return {q_id: avg_scores.get(q_id, 0.0) for q_id in auto_eval_data.keys()}

############# REFUSAL FLAGS #############

def define_promptfoo_refusal_tests(
    auto_eval_data: dict, dir_path: str, auto_eval_id: str
) -> str:
    """
    Define refusal tests for a generated response using `promptfoo` test case format.

    Args:
        auto_eval_data (dict): A dictionary containing auto eval data for each question. Each question ID should map 
        to a record that includes generated response under the key 'response'.
        dir_path (str): The directory path where the test file will be saved.
        auto_eval_id (str): The ID of the auto eval being processed.

    Returns:
        str: The file path of the generated test file.
    """

    tests = []
    for question_id, auto_eval_record in auto_eval_data.items():

        generated_text = auto_eval_record.get("response", "")

        for assertion in ["is-refusal", "not-is-refusal"]:
            test_case = {
                "description": f"qid={question_id}|assertion={assertion}",
                "vars": {"input": generated_text},
                "assert": [{"type": assertion}],
            }

            tests.append(test_case)

    # Save the generated tests to a JSON file
    test_file_path = f"{dir_path}/refusal_tests_{auto_eval_id}.json"

    with open(test_file_path, "w") as json_file:
        json.dump(tests, json_file, indent=4)

    return test_file_path


def process_promptfoo_refusal_tests(auto_eval_data: dict, auto_eval_id: str) -> dict:
    """
    Process refusal tests for a generated response using `promptfoo` and determine if the response is a refusal based on the test results.

    Args:
        auto_eval_data (dict): A dictionary containing auto eval data for each question. 
        auto_eval_id (str): The ID of the auto eval being processed.

    Returns:
        dict: A dictionary mapping question IDs to a boolean indicating if the response is classified as a refusal based on the test results.
    """

    if not isinstance(auto_eval_data, dict):
        raise ValueError("`auto_eval_data` must be a dictionary for refusal tests.")

    config_path, test_dir_path, output_path = get_promptfoo_directories()
    results_path = os.path.join(output_path, f"refusal_tests_results_{auto_eval_id}.csv")

    # Create test file based on generated responses
    test_file_path = define_promptfoo_refusal_tests(
        auto_eval_data, test_dir_path, auto_eval_id
    )

    # Run promptfoo evals
    result = run_promptfoo_tests(config_path, test_file_path, results_path)

    # Only raise on actual execution errors, not test failures
    if not os.path.exists(results_path):
        raise RuntimeError(
            f"`promptfoo` eval failed — no results file created:\n{result.stderr}"
        )

    # Parse results and calculate refusal score
    df_scores = pd.read_csv(results_path)

    df_scores["question_id"] = df_scores["Description"].apply(lambda x: x.split("|")[0].split("=")[1])
    df_scores["assertion"] = df_scores["Description"].apply(lambda x: x.split("|")[1].split("=")[1])

    df_scores_pivot = df_scores.pivot(index="question_id", columns="assertion", values="Score").reset_index()

    # Determine if a response is classified as a refusal based on the test results
    df_scores_pivot["refusal_score"] = df_scores_pivot.apply(
        lambda row: (
            None if pd.isna(row.get("is-refusal")) or pd.isna(row.get("not-is-refusal"))
            else (
                1.0
                if row["is-refusal"] == 1.0 and row["not-is-refusal"] == 0.0
                else 0.0
            )
        ),
        axis=1,
    )

    refusal_scores = df_scores_pivot.set_index("question_id")["refusal_score"].to_dict()
    print(f"Refusal tests complete — total refusals: {sum(1 for score in refusal_scores.values() if score == 1.0)}")

    return refusal_scores


def run_promptfoo_tests(config_path, test_file_path, results_path):
    """
    Runs `promptfoo` tests using the specified configuration and test file,
    and saves the results to the specified output path.
    """

    print("Running promptfoo tests...")

    return subprocess.run(
        [
            "promptfoo",
            "eval",
            "--config",
            config_path,
            "--tests",
            test_file_path,
            "--output",
            results_path,
        ],
        capture_output=True,
        text=True,
        shell=(sys.platform == "win32"),  # list+shell=True breaks on Unix; needed on Windows to resolve npm's .cmd wrapper
        encoding="utf-8"
    )


def get_promptfoo_directories() -> tuple[str, str, str]:
    """
    Returns the necessary directory paths for `promptfoo` configuration, test files, and output results.
    """

    # Paths relative to root directory
    config_path = os.path.join("utils", "promptfoo", "promptfooconfig.yaml")
    test_dir_path = os.path.join("utils", "promptfoo", "tests")
    output_path = os.path.join("results", "promptfoo")

    # Ensure directories exist
    os.makedirs(test_dir_path, exist_ok=True)
    os.makedirs(output_path, exist_ok=True)

    return config_path, test_dir_path, output_path


############# REFERENCES - SOURCE LINKS ACCESSIBILITY, HIT RATE, RECALL #############

def find_all_urls(text: str) -> list:
    """
    Find all URLs in the given text using a regular expression.

    Args:
        text (str): The input text to search for URLs.
    Returns:
        list: A list of URLs found in the input text.
    """

    pattern = r"https?://\S+|www\.\S+"
    urls = re.findall(pattern, text)

    return [url.rstrip(".,;:()[]{}<>\"'") for url in urls] # Remove trailing punctuation


def check_url_accessibility(urls: list[str]) -> tuple[bool, list, list]:
    """
    Check if all URLs found in the generated text are accessible by making HTTP GET requests.

    Args:
        urls (list[str]): A list of URLs to be checked for accessibility.

    Returns:

        tuple: A tuple containing three elements:
                bool: True if all URLs are accessible (status code 200), False if any URL is inaccessible,
                and None if no URLs are found.
                list: A list of accessible URLs.
                list: A list of inaccessible URLs.
    """

    inaccesible_urls = []
    accesible_urls = []

    for url in urls:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code != 200:
                print(
                    f"URL '{url}' is inaccessible. Status code: {response.status_code}"
                )
                inaccesible_urls.append(url)
            else:
                accesible_urls.append(url)
        except requests.RequestException as e:
            print(f"Error accessing URL '{url}': {e}")
            inaccesible_urls.append(url)

    if inaccesible_urls:
        return False, accesible_urls, inaccesible_urls

    return True, accesible_urls, []


def evaluate_source_link_metrics(
    output_obj: dict,
) -> tuple[bool, float, float, tuple[list, list]]:
    """
    Perform source links verification by checking the accessibility of URLs found in the generated text.
    Calculate hit rate and recall for the accessible URLs based on URLs associated with retrieved document
    chunks in the output object.

    Args:
        output_obj (dict): The output object containing the generated text and retrieved document chunks
        with associated URLs.

    Returns:
        tuple: A tuple containing four elements:
                bool: True if all cited URLs are accessible, False if any cited URL is inaccessible, and None if no URLs are found.
                float: The hit rate calculated as 1.0 if at least one relevant and accessible URL is cited in the generated text, otherwise 0.0.
                float: The recall calculated as the number of relevant and accessible URLs divided by the total number of URLs associated with retrieved document chunks.
                tuple: A tuple containing two lists: accessible URLs and inaccessible URLs.

    """

    # Parse generated text and chunk URLs from the output object
    generated_text = output_obj.get("response", "")
    chunk_urls = [
        chunk.get("url", "")
        for chunk in output_obj.get("chunks", [])
        if chunk.get("url", "")
    ]

    # Find all URLs in the generated text
    urls = find_all_urls(generated_text)

    if not urls:
        print("No URLs found in the generated text.")
        return None, 0.0, 0.0, ([], [])

    # Check accessibility of all URLs found in the generated text
    all_accessible, accesible_urls, inaccesible_urls = check_url_accessibility(urls)

    # Find relevant and accessible URLs when compared with chunk URLs
    relevant_accessible_urls = set(accesible_urls) & set(chunk_urls)

    # Of all the cited URLs that are accessible, is at least one from a document in the retrieved chunks?
    hit_rate = 1.0 if relevant_accessible_urls else 0.0

    # Of all the URLs associated with retrieved document chunks, how many are cited and accessible in the generated text
    recall = len(relevant_accessible_urls) / len(set(chunk_urls)) if chunk_urls else 0.0

    return all_accessible, hit_rate, recall, (accesible_urls, inaccesible_urls)


############# CONTEXT RETRIEVAL - HIT RATE @k, RECALL @k #############

def evaluate_retrieval_at_k(output_obj: dict, k: int) -> tuple[float, float]:
    """
    Perform retrieval checks by calculating hit rate @k and recall @k for the retrieved document chunks
    in the output object compared to a list of relevant documents.

    Args:
        output_obj (dict): The output object containing the retrieved document chunks with associated S3 URIs
        and and relevant document URIs.
        k (int): The number of top retrieved document chunks to consider for the calculations.

    Returns:
        tuple: A tuple containing three elements:
                float: The hit rate @k calculated as 1.0 if at least one of the top k retrieved chunks is from a relevant document, otherwise 0.0.
                float: The recall @k calculated as the number of relevant documents in the top k retrieved chunks
                divided by the total number of relevant documents.
                float: The reciprocal rank @k calculated as 1 divided by the rank of the first relevant document in the top k retrieved chunks, 
                or 0.0 if no relevant documents are in the top k retrieved chunks.
    """

    # Get all retrieved document chunk URIs from the output object
    retrieved_chunks = output_obj.get("chunks", [])

    # Get URIs of relevant documents
    relevant_doc_uris = output_obj.get("relevant_docs", [])

    # Handle edge cases
    if not retrieved_chunks:
        print(f"No chunks retrieved — returning 0.0 for hit rate and recall @ k={k}")
        return 0.0, 0.0, 0.0

    if not relevant_doc_uris:
        print(f"No relevant docs provided — cannot evaluate @ k={k}")
        return 0.0, 0.0, 0.0

    if k <= 0:
        raise ValueError(f"k must be a positive integer, got {k}")

    # Handle case where k is greater than the number of retrieved chunks
    actual_k = min(k, len(retrieved_chunks))
    if actual_k < k:
        print(
            f"Warning: only {actual_k} chunks retrieved, evaluating @ k={actual_k} instead of k={k}"
        )

    # Top k retrieved document URIs
    top_k_retrieved_chunks = retrieved_chunks[:actual_k]
    top_k_retrieved_doc_uris = [
        chunk.get("s3_uri", "")
        for chunk in top_k_retrieved_chunks
        if chunk.get("s3_uri", "")
    ]

    # Calculate number of relevant documents in the top k retrieved chunks
    relevant_retrieved_uris = set(top_k_retrieved_doc_uris) & set(relevant_doc_uris)

    # Hit Rate @k: Of the top k retrieved chunks, is at least one from a relevant document?
    hit_rate_at_k = 1.0 if relevant_retrieved_uris else 0.0

    # Recall @k: Of all the relevant documents, how many are in the top k retrieved chunks?
    recall_at_k = (
        len(relevant_retrieved_uris) / len(set(relevant_doc_uris))
        if relevant_doc_uris
        else 0.0
    )

    # Reciprocal rank @k
    # Calculating at chunk level, so different chunks from the same document will all be kept in the ranking. This prevents deduplication that might overstate ranking performance.
    reciprocal_rank_at_k = 0.0
    for rank, uri in enumerate(top_k_retrieved_doc_uris, start=1):
        if uri in relevant_doc_uris:
            reciprocal_rank_at_k = 1 / rank
            break

    return hit_rate_at_k, recall_at_k, reciprocal_rank_at_k


############# SUMMARY STATISTICS #############

def _collect(results_by_question: dict, eval_id: str, index: int | None = None) -> list:
    """Collect non-None scalar or tuple-indexed values for an eval_id across all questions. Function created using Claude Sonnet 4.6 via GitHub Copilot ."""
    out = []
    for qr in results_by_question.values():
        v = qr.get(eval_id)
        if v is None:
            continue
        if index is not None:
            if hasattr(v, "__len__") and len(v) > index:
                out.append(v[index])
        elif isinstance(v, (int, float)):
            out.append(v)
    return out


def auto_evals_summary_statistics(results_by_question: dict) -> dict:
    """
    Calculate summary statistics for the auto eval results across all questions.

    Args:
        results_by_question (dict): A dictionary mapping question IDs to their respective auto eval results.
    
    Returns:
        dict: A dictionary containing summary statistics such as average scores for each eval type and overall average score across all evals and questions.
    """
    summary_stats = {}

    for eval_id in AUTO_EVAL_MAP:
        eval_name = AUTO_EVAL_MAP[eval_id]

        if eval_id in ("ae-test-1", "ae-test-2", "ae-test-4"):
            scores = _collect(results_by_question, eval_id, index=2 if eval_id == "ae-test-1" else None)
            summary_stats[f"average_{eval_name}"] = float(np.mean(scores)) if scores else 0.0

        elif eval_id == "ae-test-3":
            vals = [qr.get(eval_id) for qr in results_by_question.values()]
            summary_stats["refusal_counts"] = {
                "refusal":       sum(v == 1.0 for v in vals),
                "not_refusal":   sum(v == 0.0 for v in vals),
                "undetermined":  sum(v is None for v in vals),
            }

        elif eval_id == "ae-test-5":
            for label, idx in (("average_hit_rate", 0), ("average_recall_rate", 1), ("mean_reciprocal_rank", 2)):
                vals = _collect(results_by_question, eval_id, index=idx)
                summary_stats[f"{label}_{eval_name}"] = float(np.mean(vals)) if vals else 0.0

        elif eval_id == "ae-test-6":
            summary_stats["url_accessibility_counts"] = sum(
                1 for qr in results_by_question.values()
                if qr.get(eval_id) and qr[eval_id][0] is True
            )
            for label, idx in (("average_hit_rate", 1), ("average_recall_rate", 2)):
                vals = _collect(results_by_question, eval_id, index=idx)
                summary_stats[f"{label}_{eval_name}"] = float(np.mean(vals)) if vals else 0.0

    return summary_stats


############# INTER-RATER RELIABILITY #############

def calculate_pairwise_weighted_kappa(df: pd.DataFrame, eval_col: str, evaluators: list) -> float:
    """
    Calculate weighted Cohen's Kappa for inter-rater reliability between two reviewers for a specific evaluation criterion.

    Args:
        df (pd.DataFrame): The DataFrame containing the evaluation data with columns for question_id, reviewer, and the specified eval_col.
        eval_col (str): The name of the column in the DataFrame that contains the categorical ratings to be evaluated for inter-rater reliability.
        evaluators (list): A list of evaluators to consider for the inter-rater reliability calculation.

    Returns:
        float: The calculated weighted Cohen's Kappa score indicating the level of agreement between the two reviewers for the specified evaluation criterion, 
        where a score of 1 indicates perfect agreement, 0 indicates agreement equivalent to chance, and negative values indicate less than chance agreement.
    """

    # Keep only questions with 2 responses for this eval criterion
    valid_qids = (
        df.loc[df["reviewer"].isin(evaluators)]
        .groupby("question_id")[eval_col]
        .count()
        .loc[lambda s: s == 2]
        .index
    )

    data = (
        df.loc[
            df["question_id"].isin(valid_qids) & df["reviewer"].isin(evaluators),
            ["reviewer", "question_id", eval_col]
        ]
        .dropna()
        .astype({"reviewer": str, "question_id": str, f"{eval_col}": str})
        .values
        .tolist()
    )

    if data:
        task = agreement.AnnotationTask(data=data)
        
        try:
            weighted_kappa = task.weighted_kappa()
        except ZeroDivisionError:
            weighted_kappa = float("nan")
        return weighted_kappa
    else:
        return None


def calculate_krippendorffs_alpha(df: pd.DataFrame, eval_col: str) -> float:
    """
    Calculate Krippendorff's Alpha for inter-rater reliability among multiple reviewers for a specific evaluation criterion.

    Args:
        df (pd.DataFrame): The DataFrame containing the evaluation data with columns for question_id, reviewer, and the specified eval_col.
        eval_col (str): The name of the column in the DataFrame that contains the Likert scale ratings to be evaluated for inter-rater reliability.  
    """

    data = (
        df.loc[:, ["reviewer", "question_id", eval_col]]
        .dropna()
        .astype({"reviewer": str, "question_id": str, f"{eval_col}": str})
        .values
        .tolist()
    )

    if data:
        task = agreement.AnnotationTask(data=data)
        
        try:
            alpha = task.alpha()
        except ZeroDivisionError:
            alpha = float("nan")
        return alpha
    else:
        return None