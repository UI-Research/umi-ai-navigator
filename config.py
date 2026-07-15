"""
Shared configuration for Bedrock Knowledge Base scripts.

This file contains configuration that should be kept in sync across:
- 02_create-bedrock-kb.py
- 03_test-bedrock-kb.py
- 04_generate-responses-for-eval.py

Update this file when adding new KB types or changing KB configurations.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Project root directory — all paths should be built from this.
# Anchored to config.py's location (which lives in the project root).
PROJECT_ROOT = Path(__file__).resolve().parent

# Box Drive paths (set in .env to your locally synced Box folders)
DATA_FOLDER = Path(os.environ.get("DATA_FOLDER", "")).expanduser()
METADATA_PATH = DATA_FOLDER / "Knowledge Base Metadata.xlsx"
EVAL_FOLDER = Path(os.environ.get("EVAL_FOLDER", "")).expanduser()
EVAL_PROMPTS_PATH = EVAL_FOLDER / "Full Evaluation Methodology.xlsx"

# Knowledge Base configurations
KB_CONFIGS = {
    'default-md': {
        'name': 'your-kb-default-md',
        'description': 'KB with default parser and markdown files',
        'documents_prefix': 'documents-md/',
        'parser': None,
        'index_suffix': 'default-md'
    },
    'automation-pdf': {
        'name': 'your-kb-automation-pdf',
        'description': 'KB with Data Automation parser and PDF files',
        'documents_prefix': 'documents-pdf/',
        'parser': 'BEDROCK_DATA_AUTOMATION',
        'index_suffix': 'automation-pdf'
    }
}

# AWS Configuration
REGION = "us-east-1"
BUCKET_NAME = "your-eval-bucket"
SHINY_BUCKET_NAME = "your-shiny-bucket"

# Embedding and Generation Models
EMBEDDING_MODEL_ARN = "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
GENERATION_MODEL_ARN_LIGHT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"  # Haiku 4.5                                                                                                                                
GENERATION_MODEL_ARN       = "us.anthropic.claude-sonnet-4-6"               # Sonnet 4.6  
FILTER_GENERATION_MODEL_ARN = "us.anthropic.claude-haiku-4-5-20251001-v1:0" # Use Haiku 4.5 because Sonnet 3.5 (used in AWS docs) was deprecated

# Metadata and Prompts
METADATA_SCHEMA_S3_URI = ""
SYSTEM_PROMPT_PATH = PROJECT_ROOT/"prompts"/"system-prompt.md"
