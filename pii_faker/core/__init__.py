"""
Core module for PII Faker.

Contains the main processing logic for PII detection, generation, redaction,
and restoration. All DataFrame transforms go through the single pipeline in
pii_faker.core.pipeline (plan -> apply -> mapping).
"""

from pii_faker.core.detector import detect_pii_columns, PIIColumnDetector
from pii_faker.core.generators import PIIGenerator, FakeDataGenerator
from pii_faker.core.redactor import (
    PIIRedactor,
    RedactionStyle,
    TOKEN_PATTERN,
    make_typed_token,
    typed_token_label,
)
from pii_faker.core.mapper import PIIMapper, MappingStore
from pii_faker.core.restorer import PIIRestorer
from pii_faker.core.file_io import FileHandler
from pii_faker.core.json_walker import JsonWalker
from pii_faker.core.config import (
    PIIConfig,
    PIIMode,
    MatchStrategy,
    load_user_extra_columns,
    with_user_columns,
)
from pii_faker.core.state import RunState
from pii_faker.core.values import dtype_name, parse_dtype
from pii_faker.core import pipeline as _pipeline
from pii_faker.core.pipeline import (
    Target,
    PlanResult,
    plan_dataframe,
    apply_plan,
    process_sheets,
    restore_sheets,
    detect_targets,
)

__all__ = [
    "detect_pii_columns",
    "PIIColumnDetector",
    "PIIGenerator",
    "FakeDataGenerator",
    "PIIRedactor",
    "RedactionStyle",
    "TOKEN_PATTERN",
    "make_typed_token",
    "typed_token_label",
    "PIIMapper",
    "MappingStore",
    "PIIRestorer",
    "FileHandler",
    "JsonWalker",
    "PIIConfig",
    "PIIMode",
    "MatchStrategy",
    "load_user_extra_columns",
    "with_user_columns",
    "RunState",
    "dtype_name",
    "parse_dtype",
    "Target",
    "PlanResult",
    "plan_dataframe",
    "apply_plan",
    "process_sheets",
    "restore_sheets",
    "detect_targets",
    "process_dataframe",
    "restore_dataframe",
]


def process_dataframe(
    df: "pl.DataFrame",
    config: PIIConfig,
    mapping_out: str | None = None,
    selected: list[str] | None = None,
    scope: str = "",
    mapper: PIIMapper | None = None,
    run_state: RunState | None = None,
) -> tuple["pl.DataFrame", PIIMapper]:
    """
    Process a DataFrame to fake or redact PII columns.

    Single pipeline (plan -> apply -> mapping) shared by the CLI, web UI
    and Python API.

    Args:
        df: Input Polars DataFrame
        config: PII configuration
        mapping_out: Optional path to save the mapping file
        selected: Optional explicit paths to process
        scope: Mapping scope (sheet/file name)
        mapper: Optional existing mapper to record into
        run_state: Optional shared run state

    Returns:
        Tuple of (processed DataFrame, mapper with mappings)
    """
    return _pipeline.process_dataframe(
        df,
        config,
        mapping_out=mapping_out,
        selected=selected,
        scope=scope,
        mapper=mapper,
        run_state=run_state,
    )


def restore_dataframe(
    df: "pl.DataFrame",
    mapper: PIIMapper,
    scope: str = "",
) -> "pl.DataFrame":
    """
    Restore original values in a DataFrame using a mapping.

    Args:
        df: DataFrame with faked/redacted values
        mapper: PIIMapper containing the mappings
        scope: Mapping scope (sheet/file name)

    Returns:
        DataFrame with original values restored
    """
    return _pipeline.restore_dataframe(df, mapper, scope=scope)
