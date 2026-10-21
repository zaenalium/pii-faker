"""
PII redaction module.

Provides multiple redaction styles: full, partial, token, and typed-token.
Token styles use a shared per-run counter so tokens are globally unique
and restore deterministically across columns and sheets.
"""

import re
from typing import Dict, Any, Tuple, Optional

import polars as pl

from pii_faker.core.config import PIIConfig, RedactionStyle
from pii_faker.core.state import RunState

TOKEN_PATTERN = re.compile(r"^(REDACTED_\d{4}|<[A-Z_]+_\d+>)$")


def typed_token_label(category: str) -> str:
    """Map a PII category to its typed-token label."""
    labels = {
        "name": "PERSON",
        "contact": "CONTACT",
        "address": "ADDRESS",
        "identifier": "ID",
        "date": "DATE",
    }
    return labels.get(category, "PII")


def make_typed_token(category: str, n: int, field_name: str = "") -> str:
    """Build a typed token like <PERSON_1> for a category."""
    field = (field_name or "").lower()
    if "email" in field:
        label = "EMAIL"
    elif "phone" in field:
        label = "PHONE"
    elif category == "contact":
        label = "CONTACT"
    else:
        label = typed_token_label(category)
    return f"<{label}_{n}>"


class PIIRedactor:
    """Redacts PII values with configurable styles."""

    def __init__(self, config: PIIConfig, run_state: Optional[RunState] = None):
        self.config = config
        self.run_state = run_state or RunState()
        self.token_counter = 0

    @property
    def _counter(self) -> int:
        return self.run_state.token_counter

    def _redact_full(self, value: str) -> str:
        """Full redaction: replace with [REDACTED]."""
        return "[REDACTED]"

    def _redact_partial(self, value: str) -> str:
        """Partial redaction: show last 4 chars, mask the rest."""
        if len(value) <= 4:
            return "X" * len(value)
        return "X" * (len(value) - 4) + value[-4:]

    def _next_token_number(self) -> int:
        n = self.run_state.next_token()
        self.token_counter = n
        return n

    def _redact_token(self, value: str) -> str:
        """Token redaction: replace with globally unique token."""
        return f"REDACTED_{self._next_token_number():04d}"

    def _redact_typed_token(self, value: str, category: str = "custom", field_name: str = "") -> str:
        """Typed-token redaction: <PERSON_1>, <EMAIL_3>, ..."""
        return make_typed_token(category, self._next_token_number(), field_name)

    def _apply_redaction(self, value: str, style: RedactionStyle, category: str = "custom", field_name: str = "") -> str:
        """Apply redaction style to a single value."""
        if style == RedactionStyle.FULL:
            return self._redact_full(value)
        elif style == RedactionStyle.PARTIAL:
            return self._redact_partial(value)
        elif style == RedactionStyle.TOKEN:
            return self._redact_token(value)
        elif style == RedactionStyle.TYPED_TOKEN:
            return self._redact_typed_token(value, category, field_name)
        else:
            return self._redact_full(value)

    def build_mapping(
        self,
        series: pl.Series,
        field_name: str = "",
        category: str = "custom",
    ) -> Dict[str, str]:
        """
        Build a mapping from original values to redacted values.

        Tokens are globally unique across the run (counter lives in the
        shared RunState), so multi-column restores stay lossless.

        Args:
            series: Polars Series with original values
            field_name: Name of the field
            category: PII category for typed tokens

        Returns:
            Dict mapping original -> redacted
        """
        unique_values = sorted(series.drop_nulls().unique().to_list(), key=str)

        mapping = {}
        for original in unique_values:
            if isinstance(original, str):
                redacted = self._apply_redaction(
                    original, self.config.redact_style, category, field_name
                )
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
        col_name = column_info["column"]
        category = column_info.get("category", "custom")

        mapping = self.build_mapping(df[col_name], col_name, category)

        if not mapping:
            return df, {}

        df = df.with_columns(pl.col(col_name).replace(mapping).alias(col_name))

        return df, mapping
