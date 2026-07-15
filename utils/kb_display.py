"""Display/formatting functions for Knowledge Base retrieval and generation results.

These functions handle all terminal output formatting for 03_test-bedrock-kb.py.
They have no AWS dependencies — they only consume response dicts and print them.
"""

import json


INTERACTIVE_HELP = """
Commands (with cost indicators):
  <query>                  - Retrieve and generate response [💰💰 Filter + Generation]
  /retrieve <query>        - Retrieve only, with filtering [💰 Filter only]
  /retrieve-nofilter <q>   - Retrieve only, no filtering [💲 Cheapest - retrieval only]
  /compare <query>         - Compare filtered vs unfiltered retrieval [💰💰 2x Retrieval + Filter]
  /nofilter <query>        - Query without metadata filtering [💰 Generation only]
  /cite <query>            - Generate with URL citations [💰💰 Filter + Generation]
  /help                    - Show this help
  /quit                    - Exit

Cost breakdown (heuristic - actual costs depend on model, prompt length, and API pricing):
  💲   = Cheapest (retrieval only, no filter, no generation)
  💰   = Low cost (single operation)
  💰💰  = Moderate cost (filter generation + another operation)
  💰💰💰+ = Higher cost (multiple API calls)
"""


def print_retrieval_results(response: dict, title: str = "Retrieval Results"):
    """Pretty print retrieval results from retrieve_chunks()."""
    print(f"\n{'='*60}")
    print(f" {title}")
    print('='*60)

    results = response.get('retrievalResults', [])
    print(f"\nFound {len(results)} chunks:\n")

    for i, result in enumerate(results, 1):
        print(f"--- Chunk {i} ---")

        # Source location
        location = result.get('location', {})
        if 's3Location' in location:
            uri = location['s3Location'].get('uri', 'N/A')
            print(f"Source: {uri}")

        # Score
        score = result.get('score', 'N/A')
        print(f"Score: {score}")

        # Metadata
        metadata = result.get('metadata', {})
        if metadata:
            print(f"Metadata: {json.dumps(metadata, indent=2)}")

        # Content (truncated)
        content = result.get('content', {}).get('text', '')
        if len(content) > 500:
            content = content[:500] + "..."
        print(f"Content:\n{content}")
        print()


def print_generation_results(response: dict):
    """Pretty print results from retrieve_and_generate()."""
    print(f"\n{'='*60}")
    print(" Generated Response")
    print('='*60)

    # Generated text
    output = response.get('output', {})
    generated_text = output.get('text', 'No response generated')
    print(f"\n{generated_text}\n")

    # Citations
    citations = response.get('citations', [])
    if citations:
        print(f"\n{'='*60}")
        print(" Citations & Retrieved Chunks")
        print('='*60)

        for i, citation in enumerate(citations, 1):
            print(f"\n--- Citation {i} ---")

            # Generated response span
            gen_span = citation.get('generatedResponsePart', {}).get('textResponsePart', {})
            if gen_span:
                span_text = gen_span.get('text', '')[:200]
                print(f"Response span: \"{span_text}...\"")

            # Retrieved references
            refs = citation.get('retrievedReferences', [])
            for j, ref in enumerate(refs, 1):
                print(f"\n  Reference {j}:")

                # Source
                location = ref.get('location', {})
                if 's3Location' in location:
                    uri = location['s3Location'].get('uri', 'N/A')
                    print(f"    Source: {uri}")

                # Metadata
                metadata = ref.get('metadata', {})
                if metadata:
                    # Filter out verbose internal metadata
                    display_metadata = {k: v for k, v in metadata.items()
                                       if not k.startswith('AMAZON_BEDROCK')}
                    if display_metadata:
                        print(f"    Metadata: {json.dumps(display_metadata, indent=6)}")

                # Content snippet
                content = ref.get('content', {}).get('text', '')
                if len(content) > 300:
                    content = content[:300] + "..."
                print(f"    Content: {content}")


def print_comparison_summary(unfiltered_results: list, filtered_results: list):
    """Print a doc-level diff and average score comparison between two retrieval result lists.

    Args:
        unfiltered_results: List of retrievalResults dicts (from response['retrievalResults'])
        filtered_results: List of retrievalResults dicts
    """
    # Document-level diff
    def _doc_names(results):
        docs = set()
        for r in results:
            loc = r.get('location', {}).get('s3Location', {}).get('uri', '')
            if loc:
                docs.add(loc.split('/')[-1])
        return docs

    unfiltered_docs = _doc_names(unfiltered_results)
    filtered_docs = _doc_names(filtered_results)

    print(f"\n{'='*60}")
    print(" Comparison Summary")
    print('='*60)

    if unfiltered_docs == filtered_docs:
        print(f"\n✓ Same documents in both ({len(unfiltered_docs)} docs)")
    else:
        only_unfiltered = unfiltered_docs - filtered_docs
        only_filtered = filtered_docs - unfiltered_docs
        if only_unfiltered:
            print(f"\nExcluded by filter: {only_unfiltered}")
        if only_filtered:
            print(f"Only in filtered: {only_filtered}")

    # Average score comparison
    def _avg_score(results):
        scores = [r.get('score') for r in results if r.get('score') is not None]
        return sum(scores) / len(scores) if scores else None

    unfiltered_avg = _avg_score(unfiltered_results)
    filtered_avg = _avg_score(filtered_results)

    if unfiltered_avg is not None and filtered_avg is not None:
        diff = filtered_avg - unfiltered_avg
        direction = "+" if diff > 0 else ""
        print(f"\nAvg scores: unfiltered={unfiltered_avg:.4f}, filtered={filtered_avg:.4f} ({direction}{diff:.4f})")


def print_generation_with_urls_results(response: dict):
    """Pretty print results from retrieve_and_generate_with_urls()."""
    print(f"\n{'='*60}")
    print(" Generated Response (with URL Citations)")
    print('='*60)

    print(f"\n{response['generated_text']}\n")

    # Print source summary
    sources = response.get('sources', [])
    if sources:
        print(f"\n{'='*60}")
        print(" Source Details")
        print('='*60)

        for source in sources:
            print(f"\n[Source {source['index']}]")
            print(f"  URL: {source['url']}")
            print(f"  Score: {source['score']}")
            if source['metadata']:
                print(f"  Metadata: {json.dumps(source['metadata'], indent=4)}")