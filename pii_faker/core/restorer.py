"""
PII restoration module.

Restores original values from faked/redacted data using mappings.

Tabular cells are restored by exact match on the whole value. Substring
replacement would corrupt data whenever one fake is a prefix of another
(fake "123" inside fake "1234"), which fixed-width identifiers make common.
"""

from typing import Dict, Any, Optional, List, Union

import polars as pl

from pii_faker.core.json_walker import JsonWalker
from pii_faker.core.mapper import PIIMapper


class PIIRestorer:
    """Restores original PII values from faked/redacted data."""

    def __init__(self, mapper: PIIMapper):
        self.mapper = mapper
        self._reverse_mapping = mapper.store.get_reverse_mapping()
        self._reverse_by_column = mapper.store.get_reverse_mapping_by_column()

    def restore_value(self, value: str) -> str:
        """Restore a single value using reverse mapping."""
        if not isinstance(value, str):
            return value
        return self._reverse_mapping.get(value, value)

    def restore_series(self, series: pl.Series, column: Optional[str] = None) -> pl.Series:
        """
        Restore a Polars Series by exact match on each value.

        Args:
            series: Series with faked/redacted values
            column: Optional column name for scoped mapping

        Returns:
            Series with original values restored
        """
        # Get appropriate reverse mapping
        if column and column in self._reverse_by_column:
            reverse_map = self._reverse_by_column[column]
        else:
            reverse_map = self._reverse_mapping

        if not reverse_map:
            return series

        return series.replace(reverse_map)

    def restore_dataframe(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Restore all mapped columns in a DataFrame.

        Handles flat columns, nested struct/list columns (mapping key is the
        path, e.g. `patient.contacts[*].email`) and JSON documents stored in
        text cells (mapping key is `column:json.path`).

        Args:
            df: DataFrame with faked/redacted values

        Returns:
            DataFrame with original values restored
        """
        json_rules: Dict[str, List] = {}

        for key, reverse_map in self._reverse_by_column.items():
            if not reverse_map:
                continue

            if key not in df.columns and ':' in key:
                column, path = key.split(':', 1)
                if column in df.columns and df.schema[column] == pl.String:
                    json_rules.setdefault(column, []).append((path, reverse_map))
                continue

            if key in df.columns and df.schema[key] == pl.String:
                df = df.with_columns(pl.col(key).replace(reverse_map))
                continue

            expr = JsonWalker.rewrite_expr(df.schema, key, reverse_map)
            if expr is not None:
                df = df.with_columns(expr)

        for column, rules in json_rules.items():
            df = JsonWalker.rewrite_json_column(df, column, rules)

        return df

    def restore_text(self, text: str) -> str:
        """
        Restore faked/redacted values in free-form text.

        Uses the full reverse mapping to find and replace all known
        faked values in the text.

        Args:
            text: Text containing faked/redacted values

        Returns:
            Text with original values restored
        """
        if not self._reverse_mapping:
            return text

        # Sort by length descending to replace longer patterns first
        # This prevents partial replacements (e.g., "REDACTED_001" being partially replaced)
        sorted_patterns = sorted(
            self._reverse_mapping.keys(),
            key=len,
            reverse=True
        )

        result = text
        for pattern in sorted_patterns:
            original = self._reverse_mapping[pattern]
            result = result.replace(pattern, original)

        return result

    def restore_json(self, data: Any) -> Any:
        """
        Recursively restore values in a JSON-like structure.

        Args:
            data: JSON data (dict, list, or primitive)

        Returns:
            Data with original values restored
        """
        if isinstance(data, str):
            return self.restore_value(data)
        elif isinstance(data, dict):
            return {k: self.restore_json(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.restore_json(item) for item in data]
        else:
            return data

    def restore_ndjson(self, df: pl.DataFrame) -> pl.DataFrame:
        """Restore a DataFrame read from NDJSON (nested columns are handled by restore_dataframe)."""
        return self.restore_dataframe(df)
