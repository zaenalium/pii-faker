"""
PII restoration module.

Restores original values from faked/redacted data using mappings.
Uses Polars str.replace_many() for efficient batch replacement.
"""

from typing import Dict, Any, Optional, List, Union

import polars as pl

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
        Restore a Polars Series using str.replace_many().

        This uses Aho-Corasick algorithm for efficient multi-pattern replacement
        in a single pass, rather than iterated regex.

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

        # Use str.replace_many for efficient batch replacement
        patterns = list(reverse_map.keys())
        replacements = [reverse_map[p] for p in patterns]

        return series.str.replace_many(patterns, replacements)

    def restore_dataframe(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        Restore all mapped columns in a DataFrame.

        Args:
            df: DataFrame with faked/redacted values

        Returns:
            DataFrame with original values restored
        """
        if not self._reverse_mapping:
            return df

        # Get list of columns that have mappings
        mapped_columns = list(self._reverse_by_column.keys())
        columns_to_restore = [c for c in mapped_columns if c in df.columns]

        if not columns_to_restore:
            return df

        # Apply restoration to each mapped column
        expressions = []
        for col in columns_to_restore:
            reverse_map = self._reverse_by_column[col]
            if reverse_map:
                patterns = list(reverse_map.keys())
                replacements = [reverse_map[p] for p in patterns]
                expressions.append(
                    pl.col(col).str.replace_many(patterns, replacements).alias(col)
                )

        if expressions:
            df = df.with_columns(expressions)

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
        """
        Restore values in NDJSON DataFrame with nested structures.

        Handles nested structs and lists by walking the schema.

        Args:
            df: NDJSON DataFrame with nested structures

        Returns:
            DataFrame with restored values
        """
        if not self._reverse_mapping:
            return df

        # Build expressions for each column based on its type
        expressions = []
        for col_name, col_type in df.schema.items():
            expr = self._build_restore_expr(pl.col(col_name), col_type, col_name)
            if expr is not None:
                expressions.append(expr.alias(col_name))

        if expressions:
            df = df.with_columns(expressions)

        return df

    def _build_restore_expr(
        self,
        expr: pl.Expr,
        dtype: pl.DataType,
        path: str,
    ) -> Optional[pl.Expr]:
        """Build a restore expression for a column based on its type."""
        # Check if this path has a mapping
        reverse_map = self._reverse_by_column.get(path, {})

        if isinstance(dtype, pl.Utf8) and reverse_map:
            patterns = list(reverse_map.keys())
            replacements = [reverse_map[p] for p in patterns]
            return expr.str.replace_many(patterns, replacements)

        elif isinstance(dtype, pl.Struct):
            # For structs, recurse into fields
            field_exprs = []
            for field in dtype.fields:
                field_expr = self._build_restore_expr(
                    expr.struct.field(field.name),
                    field.dtype,
                    f"{path}.{field.name}",
                )
                if field_expr is not None:
                    field_exprs.append(field_expr.struct(field.name))

            if field_exprs:
                return expr.struct.with_fields(field_exprs)

        elif isinstance(dtype, pl.List) and isinstance(dtype.inner, pl.Struct):
            # For list of structs, use list.eval to restore each element
            inner_struct = dtype.inner
            field_exprs = []
            for field in inner_struct.fields:
                field_expr = self._build_restore_expr(
                    pl.element().struct.field(field.name),
                    field.dtype,
                    f"{path}[*].{field.name}",
                )
                if field_expr is not None:
                    field_exprs.append(field_expr.struct(field.name))

            if field_exprs:
                return expr.list.eval(
                    pl.element().struct.with_fields(field_exprs)
                )

        return None
