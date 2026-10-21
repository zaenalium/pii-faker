"""
Core module for PII Faker.

Contains the main processing logic for PII detection, generation, redaction,
and restoration.
"""

from pii_faker.core.detector import detect_pii_columns, PIIColumnDetector
from pii_faker.core.generators import PIIGenerator, FakeDataGenerator
from pii_faker.core.redactor import PIIRedactor, RedactionStyle
from pii_faker.core.mapper import PIIMapper, MappingStore
from pii_faker.core.restorer import PIIRestorer
from pii_faker.core.processor import PIIProcessor
from pii_faker.core.file_io import FileHandler
from pii_faker.core.json_walker import JsonWalker
from pii_faker.core.config import PIIConfig, PIIMode, MatchStrategy

__all__ = [
    "detect_pii_columns",
    "PIIColumnDetector",
    "PIIGenerator",
    "FakeDataGenerator",
    "PIIRedactor",
    "RedactionStyle",
    "PIIMapper",
    "MappingStore",
    "PIIRestorer",
    "PIIProcessor",
    "FileHandler",
    "JsonWalker",
    "PIIConfig",
    "PIIMode",
    "MatchStrategy",
]


def process_dataframe(
    df: "pl.DataFrame",
    config: PIIConfig,
    mapping_out: str | None = None,
) -> tuple["pl.DataFrame", PIIMapper]:
    """
    Process a DataFrame to fake or redact PII.

    Covers flat columns, nested struct/list columns, and (via
    `config.json_columns`) JSON documents stored as text.

    Args:
        df: Input Polars DataFrame
        config: PII configuration
        mapping_out: Optional path to save the mapping file

    Returns:
        Tuple of (processed DataFrame, mapper with mappings)
    """
    processor = PIIProcessor(config)
    df = processor.process(df)

    if mapping_out:
        processor.mapper.save(mapping_out)

    return df, processor.mapper


def restore_dataframe(
    df: "pl.DataFrame",
    mapper: PIIMapper,
) -> "pl.DataFrame":
    """
    Restore original values in a DataFrame using a mapping.

    Args:
        df: DataFrame with faked/redacted values
        mapper: PIIMapper containing the mappings

    Returns:
        DataFrame with original values restored
    """
    restorer = PIIRestorer(mapper)
    return restorer.restore_dataframe(df)
