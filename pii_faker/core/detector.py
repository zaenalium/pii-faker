"""
PII column detection module.

Detects columns that likely contain PII based on column name matching.
Supports exact, normalized, and fuzzy matching strategies.
"""

import re
from pathlib import Path
from typing import List, Set, Optional, Dict, Any

import polars as pl
import yaml

from pii_faker.core.config import PIIConfig, MatchStrategy


# Type alias for PII column info
PIIColumnInfo = Dict[str, Any]


def _normalize_name(name: str) -> str:
    """Normalize a column name for matching: lowercase, remove spaces/underscores/hyphens."""
    return re.sub(r'[\s_\-]+', '', name.lower())


def _load_default_pii_columns() -> List[Dict[str, str]]:
    """Load default PII column patterns from YAML resource."""
    yaml_path = Path(__file__).parent.parent / "data" / "default_pii_columns.yaml"
    if not yaml_path.exists():
        return []

    with open(yaml_path, 'r') as f:
        data = yaml.safe_load(f)

    return data.get('columns', [])


class PIIColumnDetector:
    """Detects PII columns in DataFrames based on column name patterns."""

    def __init__(self, config: PIIConfig):
        self.config = config
        self.default_patterns = _load_default_pii_columns()
        self._compiled_patterns = self._compile_patterns()

    def _compile_patterns(self) -> List[Dict[str, Any]]:
        """Compile all patterns (default + extra) for matching."""
        patterns = []

        if not self.config.override_defaults:
            for entry in self.default_patterns:
                pattern = entry['pattern']
                normalized = _normalize_name(pattern)
                patterns.append({
                    'original': pattern,
                    'normalized': normalized,
                    'category': entry.get('category', 'unknown'),
                    'description': entry.get('description', ''),
                })

        for extra in self.config.extra_pii_columns:
            normalized = _normalize_name(extra)
            patterns.append({
                'original': extra,
                'normalized': normalized,
                'category': 'custom',
                'description': f'User-defined: {extra}',
            })

        return patterns

    def _match_exact(self, col_name: str) -> Optional[Dict[str, Any]]:
        """Exact match: column name must match pattern exactly."""
        for pattern in self._compiled_patterns:
            if col_name == pattern['original']:
                return pattern
        return None

    def _match_normalized(self, col_name: str) -> Optional[Dict[str, Any]]:
        """Normalized match: case-insensitive, ignore spaces/underscores/hyphens."""
        normalized_col = _normalize_name(col_name)
        for pattern in self._compiled_patterns:
            if normalized_col == pattern['normalized']:
                return pattern
        return None

    def _match_fuzzy(self, col_name: str, threshold: float = 0.8) -> Optional[Dict[str, Any]]:
        """Fuzzy match: use simple substring and similarity matching."""
        normalized_col = _normalize_name(col_name)

        for pattern in self._compiled_patterns:
            normalized_pattern = pattern['normalized']

            # Check if pattern is contained in column name or vice versa
            if normalized_pattern in normalized_col or normalized_col in normalized_pattern:
                return pattern

            # Simple character overlap ratio
            if len(normalized_col) > 0 and len(normalized_pattern) > 0:
                common = set(normalized_col) & set(normalized_pattern)
                ratio = len(common) / max(len(set(normalized_col)), len(set(normalized_pattern)))
                if ratio >= threshold:
                    return pattern

        return None

    def _match_column(self, col_name: str) -> Optional[Dict[str, Any]]:
        """Match a column name against patterns using configured strategy."""
        strategy = self.config.match_strategy

        if strategy == MatchStrategy.EXACT:
            return self._match_exact(col_name)
        elif strategy == MatchStrategy.NORMALIZED:
            return self._match_normalized(col_name)
        elif strategy == MatchStrategy.FUZZY:
            return self._match_fuzzy(col_name)
        else:
            return self._match_normalized(col_name)

    def detect(self, df: pl.DataFrame) -> List[PIIColumnInfo]:
        """
        Detect PII columns in a DataFrame.

        Args:
            df: Polars DataFrame to scan

        Returns:
            List of dicts with column info: name, category, pattern_match
        """
        pii_columns = []

        for col_name in df.columns:
            match = self._match_column(col_name)
            if match:
                pii_columns.append({
                    'column': col_name,
                    'category': match['category'],
                    'pattern': match['original'],
                    'description': match['description'],
                })

        return pii_columns

    def detect_nested_paths(
        self,
        schema: Dict[str, Any],
        parent_path: str = "",
        json_columns: Optional[List[str]] = None,
    ) -> List[PIIColumnInfo]:
        """
        Detect PII paths in a nested schema structure.

        Args:
            schema: Schema dict from Polars (e.g., from df.schema)
            parent_path: Current path prefix for nested fields
            json_columns: List of columns containing JSON strings

        Returns:
            List of PII paths with metadata
        """
        pii_paths = []

        for field_name, field_type in schema.items():
            current_path = f"{parent_path}.{field_name}" if parent_path else field_name

            # Check if this field name matches PII
            match = self._match_column(field_name)
            if match:
                pii_paths.append({
                    'path': current_path,
                    'field': field_name,
                    'category': match['category'],
                    'pattern': match['original'],
                    'description': match['description'],
                })

            # Recurse into struct fields
            if hasattr(field_type, 'fields'):
                nested_schema = {f.name: f.dtype for f in field_type.fields}
                pii_paths.extend(
                    self.detect_nested_paths(nested_schema, current_path, json_columns)
                )
            # Recurse into list of structs
            elif hasattr(field_type, 'inner') and hasattr(field_type.inner, 'fields'):
                nested_schema = {f.name: f.dtype for f in field_type.inner.fields}
                pii_paths.extend(
                    self.detect_nested_paths(nested_schema, current_path, json_columns)
                )

        return pii_paths


def detect_pii_columns(
    df: pl.DataFrame,
    config: PIIConfig,
) -> List[PIIColumnInfo]:
    """
    Convenience function to detect PII columns.

    Args:
        df: Polars DataFrame
        config: PII configuration

    Returns:
        List of detected PII column info
    """
    detector = PIIColumnDetector(config)
    return detector.detect(df)
