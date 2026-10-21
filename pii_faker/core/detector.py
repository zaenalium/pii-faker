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
from pii_faker.core.value_detectors import detect_value_shapes


# Type alias for PII column info
PIIColumnInfo = Dict[str, Any]

# Column-name tokens that mark a multi-token name as organisational rather
# than personal (roadmap 2.4: `company_name` must not fuzzy-match `name`).
NON_PII_QUALIFIERS = frozenset({
    "company", "companies", "corporate", "business", "employer",
    "organisation", "organization", "firm",
})


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
        """Fuzzy match: token-based matching against pattern token sets."""
        tokens = set(re.findall(r"[a-z0-9]+", col_name.lower()))
        if not tokens:
            return None
        if len(tokens) > 1 and tokens & NON_PII_QUALIFIERS:
            return None

        best: Optional[Dict[str, Any]] = None
        best_score = 0.0
        for pattern in self._compiled_patterns:
            pattern_tokens = set(re.findall(r"[a-z0-9]+", pattern["original"].lower()))
            if not pattern_tokens:
                continue
            overlap = tokens & pattern_tokens
            if not overlap:
                continue
            score = len(overlap) / len(pattern_tokens)
            if score > best_score:
                best_score = score
                best = pattern

        if best is not None and best_score >= threshold:
            return best
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

    def detect_with_confidence(
        self,
        df: pl.DataFrame,
        sample_size: int = 500,
        min_value_ratio: float = 0.3,
    ) -> List[PIIColumnInfo]:
        """
        Detect PII columns with confidence scores (roadmap 2.2).

        Confidence combines the name signal (1.0 for an explicit match,
        0.0 otherwise) with the best value-shape ratio:

            confidence = 0.5 * name_hit + 0.5 * value_ratio

        Columns are included when they match by name OR when a value
        detector fires at min_value_ratio. Results are sorted by
        confidence descending.
        """
        from pii_faker.core.value_detectors import MIN_RATIO

        findings: List[PIIColumnInfo] = []
        for col_name in df.columns:
            match = self._match_column(col_name)
            value_hits = detect_value_shapes(
                df[col_name], min_value_ratio, sample_size
            )
            best_ratio = max((h["ratio"] for h in value_hits), default=0.0)
            best_hit = max(value_hits, key=lambda h: h["ratio"], default=None)

            if match is None and not value_hits:
                continue
            if match is None and best_ratio < max(min_value_ratio, MIN_RATIO):
                continue

            confidence = 0.5 * (1.0 if match else 0.0) + 0.5 * best_ratio
            category = match["category"] if match else best_hit["category"]
            findings.append({
                "column": col_name,
                "category": category,
                "pattern": match["original"] if match else f"value:{best_hit['detector']}",
                "description": match["description"] if match else best_hit["description"],
                "confidence": round(confidence, 3),
                "name_match": bool(match),
                "value_detector": best_hit["detector"] if best_hit else None,
                "value_ratio": round(best_ratio, 3),
            })

        findings.sort(key=lambda f: f["confidence"], reverse=True)
        return findings

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
