"""Pytest configuration and fixtures for PII Toolkit tests."""

import pytest
import polars as pl
from pathlib import Path

from pii_toolkit.core.config import PIIConfig, PIIMode, MatchStrategy


@pytest.fixture
def config():
    """Default PII configuration for tests."""
    return PIIConfig(mode=PIIMode.FAKE, seed=42)


@pytest.fixture
def redact_config():
    """Redaction configuration for tests."""
    return PIIConfig(mode=PIIMode.REDACT, seed=42)


@pytest.fixture
def sample_df():
    """Sample DataFrame with PII columns."""
    return pl.DataFrame({
        'patient_name': ['John Doe', 'Jane Smith', 'Bob Johnson'],
        'email': ['john@example.com', 'jane@test.com', 'bob@demo.org'],
        'phone': ['555-0101', '555-0102', '555-0103'],
        'card_number': ['4111-1111-1111-1111', '5500-0000-0000-0004', '3400-0000-0000-009'],
        'non_pii_column': ['data1', 'data2', 'data3'],
    })


@pytest.fixture
def nested_df():
    """DataFrame with nested struct columns."""
    return pl.DataFrame({
        'id': [1, 2, 3],
        'patient': [
            {'name': 'John Doe', 'phone': '555-0101'},
            {'name': 'Jane Smith', 'phone': '555-0102'},
            {'name': 'Bob Johnson', 'phone': '555-0103'},
        ],
        'metadata': [
            {'key': 'a', 'value': '1'},
            {'key': 'b', 'value': '2'},
            {'key': 'c', 'value': '3'},
        ],
    })


@pytest.fixture
def json_string_df():
    """DataFrame with JSON string columns."""
    return pl.DataFrame({
        'id': [1, 2, 3],
        'payload': [
            '{"patient_name": "John Doe", "contact": {"phone": "555-0101"}}',
            '{"patient_name": "Jane Smith", "contact": {"phone": "555-0102"}}',
            '{"patient_name": "Bob Johnson", "contact": {"phone": "555-0103"}}',
        ],
        'other': ['val1', 'val2', 'val3'],
    })


@pytest.fixture
def sample_ndjson(tmp_path):
    """Create a sample NDJSON file."""
    data = [
        {"member_code": "M-001", "patient": {"patient_name": "Jane Roe", "contacts": [{"email": "jane@example.com", "phone": "0812..."}]}},
        {"member_code": "M-002", "patient": {"patient_name": "John Doe", "contacts": [{"email": "john@example.com", "phone": "0813..."}]}},
    ]
    path = tmp_path / "test.ndjson"
    pl.DataFrame(data).write_ndjson(str(path))
    return path


@pytest.fixture
def sample_csv(tmp_path, sample_df):
    """Create a sample CSV file."""
    path = tmp_path / "test.csv"
    sample_df.write_csv(str(path))
    return path


@pytest.fixture
def sample_excel(tmp_path, sample_df):
    """Create a sample Excel file."""
    path = tmp_path / "test.xlsx"
    sample_df.write_excel(str(path))
    return path
