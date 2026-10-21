"""
Shared value-transformation helpers for PII processing.

Both the process path (pipeline) and the restore path (restorer) build
native Polars expressions from these so flat, nested and cast-and-restore
columns behave identically in both directions.
"""

import datetime
from typing import Any, Dict, List, Optional

import polars as pl


def dtype_name(dtype: pl.DataType) -> str:
    """Stable string name for a dtype (stored in mapping entries)."""
    return str(dtype)


def parse_dtype(name: Optional[str]) -> Optional[pl.DataType]:
    """Parse a stored dtype name back to a Polars dtype."""
    if not name:
        return None
    simple = {
        "String": pl.Utf8,
        "Utf8": pl.Utf8,
        "Int8": pl.Int8,
        "Int16": pl.Int16,
        "Int32": pl.Int32,
        "Int64": pl.Int64,
        "UInt8": pl.UInt8,
        "UInt16": pl.UInt16,
        "UInt32": pl.UInt32,
        "UInt64": pl.UInt64,
        "Float32": pl.Float32,
        "Float64": pl.Float64,
        "Boolean": pl.Boolean,
        "Date": pl.Date,
        "Time": pl.Time,
        "Datetime": pl.Datetime,
        "Datetime(us)": pl.Datetime("us"),
        "Datetime(ms)": pl.Datetime("ms"),
        "Datetime(ns)": pl.Datetime("ns"),
    }
    if name in simple:
        dtype = simple[name]
        return dtype() if isinstance(dtype, type) else dtype
    base, _, arg = name.partition("(")
    arg = arg.rstrip(")")
    if base == "Datetime" and arg:
        try:
            return pl.Datetime(arg.strip().strip("'\""))
        except Exception:
            return None
    return None


def stringify_mapping(mapping: Dict[Any, Any]) -> Dict[str, str]:
    """Stringify both sides of a mapping (for cast-and-restore columns)."""
    return {str(k): str(v) for k, v in mapping.items()}


def is_string_dtype(dtype: pl.DataType) -> bool:
    return dtype in (pl.Utf8, pl.String)


def is_temporal_dtype(dtype: pl.DataType) -> bool:
    return dtype in (pl.Date, pl.Datetime, pl.Time, pl.Duration) or isinstance(
        dtype, (pl.Date, pl.Datetime, pl.Time, pl.Duration)
    )


def apply_mapping_expr(expr: pl.Expr, mapping: Dict[Any, Any]) -> pl.Expr:
    """Replace values via mapping, leaving everything else untouched."""
    if not mapping:
        return expr
    return expr.replace_strict(mapping, default=expr)


def is_supported_leaf(dtype: pl.DataType) -> bool:
    """Leaf dtypes the pipeline can process (strings, numbers, temporals, bool)."""
    if is_string_dtype(dtype):
        return True
    numeric = (
        pl.Int8, pl.Int16, pl.Int32, pl.Int64,
        pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
        pl.Float32, pl.Float64, pl.Boolean,
    )
    if dtype in numeric:
        return True
    if isinstance(dtype, (pl.Date, pl.Datetime, pl.Time)):
        return True
    return False


def parse_path(path: str) -> List[str]:
    """Split a dotted PII path, keeping [*] markers attached to segments."""
    return [seg for seg in path.split(".") if seg]


def normalize_for_match(name: str) -> str:
    """Normalize one path segment for name matching."""
    import re

    return re.sub(r"[\s_\-]+", "", name.replace("[*]", "").lower())


def match_value_to_category(
    value: Any,
    category: str,
    field_name: str,
    generate_fn,
) -> Any:
    """Apply a str->str transform to one python value, preserving non-strings."""
    if isinstance(value, str):
        return generate_fn(value, category, field_name)
    return value


def coerce_series_to_strings(series: pl.Series) -> List[str]:
    """Unique non-null values of a series rendered as strings, sorted."""
    return sorted({str(v) for v in series.drop_nulls().to_list() if v is not None})


def native_from_python(value: Any, dtype: pl.DataType) -> Any:
    """Convert a restored python value back toward a column dtype."""
    if value is None:
        return None
    if is_string_dtype(dtype):
        return str(value)
    if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64,
                 pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64):
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if dtype in (pl.Float32, pl.Float64):
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if dtype == pl.Boolean or isinstance(dtype, pl.Boolean):
        if isinstance(value, bool):
            return value
        return value
    if isinstance(dtype, pl.Date) or dtype == pl.Date:
        if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            return value
        try:
            return datetime.date.fromisoformat(str(value))
        except (ValueError, TypeError):
            return value
    if isinstance(dtype, pl.Datetime) or dtype == pl.Datetime:
        if isinstance(value, datetime.datetime):
            return value
        try:
            return datetime.datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return value
    return value
