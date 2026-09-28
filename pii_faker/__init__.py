"""
PII Faker - Detect, fake, or redact PII in tabular and JSON datasets.

This package provides tools for handling Personally Identifiable Information (PII)
in datasets before sharing them with Large Language Models (LLMs).
"""

__version__ = "0.1.0"
__author__ = "PII Faker Contributors"

from pii_faker.core import (
    detect_pii_columns,
    process_dataframe,
    restore_dataframe,
    PIIConfig,
)

__all__ = [
    "__version__",
    "detect_pii_columns",
    "process_dataframe",
    "restore_dataframe",
    "PIIConfig",
]
