"""
Configuration models for PII Faker.
"""

from enum import Enum
from pathlib import Path
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
    TYPED_TOKEN = "typed-token"


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
    locale: Optional[str] = None

    infer_schema_length: Optional[int] = 100
    sheets: Optional[str] = None
    all_sheets: bool = False

    model_config = {"use_enum_values": True}


def app_dirs():
    """Central app-data directories (roadmap 1.4, via platformdirs)."""
    import platformdirs

    return {
        "config": Path(platformdirs.user_config_dir("pii-faker")),
        "data": Path(platformdirs.user_data_dir("pii-faker")),
        "logs": Path(platformdirs.user_log_dir("pii-faker")),
    }


def mappings_dir() -> Path:
    """Directory for mapping files (created on demand)."""
    path = app_dirs()["data"] / "mappings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _legacy_config_path() -> Path:
    return Path.home() / ".pii_faker" / "config.yaml"


DEFAULT_CONFIG_PATH = _legacy_config_path()


def user_config_path() -> Path:
    """Config file path: platformdirs location, falling back to legacy."""
    modern = app_dirs()["config"] / "config.yaml"
    if modern.exists():
        return modern
    if _legacy_config_path().exists():
        return _legacy_config_path()
    return modern


def load_user_extra_columns(config_path: Optional[str] = None) -> List[str]:
    """Load user-defined extra PII columns from the user config file."""
    import yaml

    path = Path(config_path) if config_path else user_config_path()
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return []
    columns = data.get("extra_pii_columns", [])
    return [c for c in columns if isinstance(c, str) and c.strip()]


def with_user_columns(config: "PIIConfig", config_path: Optional[str] = None) -> "PIIConfig":
    """Return a copy of config with user columns merged into extra_pii_columns."""
    user_cols = load_user_extra_columns(config_path)
    if not user_cols:
        return config
    merged = list(config.extra_pii_columns or [])
    for col in user_cols:
        if col not in merged:
            merged.append(col)
    return config.model_copy(update={"extra_pii_columns": merged})
