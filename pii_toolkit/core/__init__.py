"""
Core module for PII Toolkit.

Contains the main processing logic for PII detection, generation, redaction,
and restoration.
"""

from pii_toolkit.core.detector import detect_pii_columns, PIIColumnDetector
from pii_toolkit.core.generators import PIIGenerator, FakeDataGenerator
from pii_toolkit.core.redactor import PIIRedactor, RedactionStyle
from pii_toolkit.core.mapper import PIIMapper, MappingStore
from pii_toolkit.core.restorer import PIIRestorer
from pii_toolkit.core.file_io import FileHandler
from pii_toolkit.core.json_walker import JsonWalker
from pii_toolkit.core.config import PIIConfig, PIIMode, MatchStrategy

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
    Process a DataFrame to fake or redact PII columns.

    Args:
        df: Input Polars DataFrame
        config: PII configuration
        mapping_out: Optional path to save the mapping file

    Returns:
        Tuple of (processed DataFrame, mapper with mappings)
    """
    import polars as pl

    detector = PIIColumnDetector(config)
    pii_columns = detector.detect(df)

    if not pii_columns:
        return df, PIIMapper(config)

    mapper = PIIMapper(config)

    if config.mode == PIIMode.FAKE:
        generator = PIIGenerator(config)
        for col in pii_columns:
            df, col_mapper = generator.process_column(df, col)
            if col_mapper:
                mapper.add_column_mapping(col['column'], col['category'], col_mapper)
    else:
        redactor = PIIRedactor(config)
        for col in pii_columns:
            df, col_mapper = redactor.process_column(df, col)
            if col_mapper:
                mapper.add_column_mapping(col['column'], col['category'], col_mapper)

    if mapping_out:
        mapper.save(mapping_out)

    return df, mapper


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
