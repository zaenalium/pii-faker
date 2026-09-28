"""
Configuration models for PII Faker.
"""

from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field


class PIIMode(str, Enum):
    """Mode of PII handling."""
    FAKE = "fake"
    REDACT = "redact"


class RedactionStyle(str, Enum):
    """Style of redaction when mode is 'redact'."""
    FULL = "full"
    PARTIAL = "partial"
    TOKEN = "token"


class MatchStrategy(str, Enum):
    """Strategy for matching PII column names."""
    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"


class PIIConfig(BaseModel):
    """Main configuration for PII processing."""

    mode: PIIMode = PIIMode.FAKE
    redact_style: RedactionStyle = RedactionStyle.FULL
    match_strategy: MatchStrategy = MatchStrategy.NORMALIZED
    seed: Optional[int] = None

    extra_pii_columns: List[str] = Field(default_factory=list)
    override_defaults: bool = False
    json_columns: List[str] = Field(default_factory=list)
    strict_paths: bool = False

    infer_schema_length: Optional[int] = 100
    sheets: Optional[str] = None
    all_sheets: bool = False

    model_config = {"use_enum_values": True}
