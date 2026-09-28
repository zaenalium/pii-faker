"""
PII redaction module.

Provides multiple redaction styles: full, partial, and token.
"""

from typing import Dict, Any, Tuple

import polars as pl

from pii_faker.core.config import PIIConfig, RedactionStyle


class PIIRedactor:
    """Redacts PII values with configurable styles."""

    def __init__(self, config: PIIConfig):
        self.config = config
        self.token_counter = 0

    def _redact_full(self, value: str) -> str:
        """Full redaction: replace with [REDACTED]."""
        return "[REDACTED]"

    def _redact_partial(self, value: str) -> str:
        """Partial redaction: show last 4 chars, mask the rest."""
        if len(value) <= 4:
            return "X" * len(value)
        return "X" * (len(value) - 4) + value[-4:]

    def _redact_token(self, value: str) -> str:
        """Token redaction: replace with unique token."""
        self.token_counter += 1
        return f"REDACTED_{self.token_counter:04d}"

    def _apply_redaction(self, value: str, style: RedactionStyle) -> str:
        """Apply redaction style to a single value."""
        if style == RedactionStyle.FULL:
            return self._redact_full(value)
        elif style == RedactionStyle.PARTIAL:
            return self._redact_partial(value)
        elif style == RedactionStyle.TOKEN:
            return self._redact_token(value)
        else:
            return self._redact_full(value)

    def build_mapping(
        self,
        series: pl.Series,
        field_name: str = "",
    ) -> Dict[str, str]:
        """
        Build a mapping from original values to redacted values.

        Args:
            series: Polars Series with original values
            field_name: Name of the field

        Returns:
            Dict mapping original -> redacted
        """
        # Reset counter for consistent tokens per column
        self.token_counter = 0

        unique_values = series.drop_nulls().unique().to_list()

        mapping = {}
        for original in unique_values:
            if isinstance(original, str):
                redacted = self._apply_redaction(original, self.config.redact_style)
                mapping[original] = redacted

        return mapping

    def process_column(
        self,
        df: pl.DataFrame,
        column_info: Dict[str, Any],
    ) -> Tuple[pl.DataFrame, Dict[str, str]]:
        """
        Process a single column: build mapping and apply redaction.

        Args:
            df: Input DataFrame
            column_info: Dict with column, category, etc.

        Returns:
            Tuple of (modified DataFrame, mapping dict)
        """
        col_name = column_info['column']

        # Build mapping from unique values
        mapping = self.build_mapping(df[col_name], col_name)

        if not mapping:
            return df, {}

        # Apply mapping using replace (vectorized, no row-wise UDF)
        df = df.with_columns(
            pl.col(col_name).replace(mapping).alias(col_name)
        )

        return df, mapping
