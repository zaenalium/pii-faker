"""
PII column detection module.

Detects columns that likely contain PII based on column name matching.
Supports exact, normalized, and fuzzy matching strategies.
"""

import json
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


def _is_explicit_path(pattern: str) -> bool:
    """True for patterns that address a specific path (`a.b`, `a[*].b`)."""
    return '.' in pattern or '[' in pattern


def _path_key(path: str, strategy: str) -> str:
    """Comparable form of a path. `[*]` is dropped: lists are traversed transparently."""
    path = path.replace('[*]', '')
    return path if strategy == MatchStrategy.EXACT else _normalize_name(path)


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
                    'explicit': _is_explicit_path(pattern),
                })

        for extra in self.config.extra_pii_columns:
            normalized = _normalize_name(extra)
            patterns.append({
                'original': extra,
                'normalized': normalized,
                'category': 'custom',
                'description': f'User-defined: {extra}',
                'explicit': _is_explicit_path(extra),
            })

        return patterns

    def _match_exact(self, col_name: str, patterns=None) -> Optional[Dict[str, Any]]:
        """Exact match: column name must match pattern exactly."""
        for pattern in patterns if patterns is not None else self._compiled_patterns:
            if col_name == pattern['original']:
                return pattern
        return None

    def _match_normalized(self, col_name: str, patterns=None) -> Optional[Dict[str, Any]]:
        """Normalized match: case-insensitive, ignore spaces/underscores/hyphens."""
        normalized_col = _normalize_name(col_name)
        for pattern in patterns if patterns is not None else self._compiled_patterns:
            if normalized_col == pattern['normalized']:
                return pattern
        return None

    def _match_fuzzy(
        self, col_name: str, threshold: float = 0.8, patterns=None
    ) -> Optional[Dict[str, Any]]:
        """Fuzzy match: use simple substring and similarity matching."""
        normalized_col = _normalize_name(col_name)

        for pattern in patterns if patterns is not None else self._compiled_patterns:
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

    def _match_column(self, col_name: str, patterns=None) -> Optional[Dict[str, Any]]:
        """Match a column name against patterns using configured strategy."""
        strategy = self.config.match_strategy

        if strategy == MatchStrategy.EXACT:
            return self._match_exact(col_name, patterns)
        elif strategy == MatchStrategy.NORMALIZED:
            return self._match_normalized(col_name, patterns)
        elif strategy == MatchStrategy.FUZZY:
            return self._match_fuzzy(col_name, patterns=patterns)
        else:
            return self._match_normalized(col_name, patterns)

    def _match_field(self, name: str, path: str, nested: bool, depth: int) -> Optional[Dict[str, Any]]:
        """
        Match a field inside a nested structure.

        Explicit patterns (`patient.contacts[*].email`) must equal the field's
        full path. Bare patterns (`email`) match the field name at any depth,
        or only at depth 0 with strict_paths. Top-level DataFrame columns
        (nested=False) match exactly as `detect` does.
        """
        if not nested:
            return self._match_column(name)

        strategy = self.config.match_strategy
        key = _path_key(path, strategy)
        bare = [p for p in self._compiled_patterns if not p['explicit']]
        bare_match = self._match_column(name, bare)

        for pattern in self._compiled_patterns:
            if pattern['explicit'] and _path_key(pattern['original'], strategy) == key:
                # A path names *where* the PII is; if the leaf name also matches
                # a known kind (patient_name, email...), keep that category so
                # the fake looks right instead of falling back to a random word.
                if bare_match:
                    return {**pattern, 'category': bare_match['category']}
                return pattern

        if self.config.strict_paths and depth > 0:
            return None
        return bare_match

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

    def detect_all(self, df: pl.DataFrame) -> List[PIIColumnInfo]:
        """
        Detect PII in flat columns, nested struct/list columns, and JSON strings.

        Unlike `detect`, which only inspects top-level column names, this also
        walks Struct and List dtypes and the JSON documents held in
        `config.json_columns`. A match on a container (e.g. an `address`
        struct) covers every text field inside it.

        Each entry's 'column' is the key used in the mapping: the column name,
        a path such as `patient.contacts[*].email`, or `column:json.path` for
        JSON-string columns (which also carry 'json_column' and 'json_path').
        """
        json_columns = set(self.config.json_columns)
        found: List[PIIColumnInfo] = []

        for name, dtype in df.schema.items():
            if name in json_columns:
                continue
            found.extend(self._walk_field(name, dtype, name, 0, None))

        for column in self.config.json_columns:
            if column in df.schema and df.schema[column] == pl.String:
                found.extend(self._detect_json_column(df, column))

        return found

    @staticmethod
    def _entry(path: str, match: Dict[str, Any]) -> PIIColumnInfo:
        return {
            'column': path,
            'category': match['category'],
            'pattern': match['original'],
            'description': match['description'],
        }

    def _walk_field(
        self,
        name: str,
        dtype: pl.DataType,
        path: str,
        depth: int,
        inherited: Optional[Dict[str, Any]],
    ) -> List[PIIColumnInfo]:
        """Collect PII leaves under one schema field."""
        match = self._match_field(name, path, nested=depth > 0, depth=depth) or inherited

        base, wrapped = dtype, False
        while isinstance(base, pl.List):
            base, wrapped = base.inner, True

        if isinstance(base, pl.Struct):
            prefix = f"{path}[*]" if wrapped else path
            found: List[PIIColumnInfo] = []
            for field in base.fields:
                found.extend(
                    self._walk_field(field.name, field.dtype, f"{prefix}.{field.name}", depth + 1, match)
                )
            return found

        return [self._entry(path, match)] if match else []

    def _detect_json_column(self, df: pl.DataFrame, column: str) -> List[PIIColumnInfo]:
        """Find PII paths inside the JSON documents stored in a text column."""
        found: Dict[str, Dict[str, Any]] = {}

        def walk(node: Any, path: str, depth: int, inherited: Optional[Dict[str, Any]]):
            if isinstance(node, dict):
                for key, value in node.items():
                    sub = f"{path}.{key}" if path else key
                    match = self._match_field(key, f"{column}.{sub}", nested=True, depth=depth)
                    walk(value, sub, depth + 1, match or inherited)
            elif isinstance(node, list):
                for item in node:
                    walk(item, f"{path}[*]", depth, inherited)
            elif isinstance(node, str) and inherited and path:
                # `tags[*]` (a list of strings) is addressed as `tags`.
                found.setdefault(re.sub(r'(\[\*\])+$', '', path), inherited)

        for text in df[column].drop_nulls().unique().to_list():
            try:
                walk(json.loads(text), "", 0, None)
            except ValueError:
                continue

        return [
            {
                **self._entry(f"{column}:{path}", match),
                'json_column': column,
                'json_path': path,
            }
            for path, match in sorted(found.items())
        ]

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
