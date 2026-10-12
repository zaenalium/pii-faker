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
    SHIFT = "shift"
    PERTURB = "perturb"


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
    redact_style: RedactionStyle = RedactionStyle.TYPED_TOKEN
    match_strategy: MatchStrategy = MatchStrategy.NORMALIZED
    seed: Optional[int] = None

    extra_pii_columns: List[str] = Field(default_factory=list)
    override_defaults: bool = False
    json_columns: List[str] = Field(default_factory=list)
    strict_paths: bool = False
    locale: Optional[str] = None
    preset: Optional[str] = None

    infer_schema_length: Optional[int] = 100
    sheets: Optional[str] = None
    all_sheets: bool = False
    shift_days: Optional[int] = None
    perturb_pct: Optional[float] = None
    project_mapping: Optional[str] = None

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


def get_retention_days(config_path: Optional[str] = None) -> Optional[float]:
    """Retention for mapping files in days (roadmap 3.2).

    Returns None when unset (default sweep is 7d via cleanup_old_mappings).
    0 means keep forever.
    """
    import yaml

    path = Path(config_path) if config_path else user_config_path()
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return None
    val = data.get("retention_days")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def set_retention_days(days: float, config_path: Optional[str] = None) -> None:
    import yaml

    path = Path(config_path) if config_path else user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = yaml.safe_load(path.read_text()) or {} if path.exists() else {}
    except Exception:
        data = {}
    data["retention_days"] = float(days)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def auto_expire_mappings() -> int:
    """Sweep mappings_dir() using retention_days (quiet, warn on deletion)."""
    retention = get_retention_days()
    if retention is None or retention == 0:
        return 0
    from pii_faker.core.mapper import PIIMapper

    mapper = PIIMapper(PIIConfig())
    return mapper.cleanup_old_mappings(str(mappings_dir()), older_than_days=float(retention))


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
