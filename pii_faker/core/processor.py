"""
PII processing pipeline.

Ties detection, fake/redact generation and mapping together for flat columns,
nested struct/list columns, and JSON documents stored in text cells.

One processor handles a whole run (every sheet of a workbook, say):

- A value gets the same replacement wherever it appears, in any column, nested
  path or JSON cell of the same category, so the LLM can still see that two
  rows refer to the same person.
- Replacements stay distinct, so restoring is unambiguous.
- A column that appears in several sheets gets a single combined mapping.
"""

from typing import Dict, List, Optional, Set

import polars as pl

from pii_faker.core.config import PIIConfig, PIIMode
from pii_faker.core.detector import PIIColumnDetector, PIIColumnInfo
from pii_faker.core.generators import PIIGenerator
from pii_faker.core.json_walker import JsonWalker
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.redactor import PIIRedactor


class PIIProcessor:
    """Detects PII in DataFrames and fakes or redacts it, recording a mapping."""

    def __init__(self, config: PIIConfig):
        self.config = config
        self.detector = PIIColumnDetector(config)
        self.mapper = PIIMapper(config)
        self._generator = PIIGenerator(config) if config.mode == PIIMode.FAKE else None
        self._redactor = None if config.mode == PIIMode.FAKE else PIIRedactor(config)

        # Everything issued so far, per mapping key. Lets a column that shows up
        # in several sheets share one mapping instead of overwriting the first.
        self._known: Dict[str, Dict[str, str]] = {}

        # One replacement per original across all columns. Fakes are shared per
        # category (a name in `patient_name` and `member_name` match, but a
        # street called "Jordan" must not get a person's name). Redaction has no
        # categories, so a single table keeps tokens consistent.
        self._shared: Dict[Optional[str], Dict[str, str]] = {}
        self._shared_replacements: Dict[Optional[str], Set[str]] = {}

        # Keys that looked like PII by name but led to no text (a numeric phone
        # column, a mistyped path), so they were left untouched. Surfaced so
        # they don't leak silently.
        self.skipped: List[str] = []

    def detect(self, df: pl.DataFrame) -> List[PIIColumnInfo]:
        """Detect PII in flat, nested and JSON-string columns."""
        return self.detector.detect_all(df)

    def unique_count(self, df: pl.DataFrame, info: PIIColumnInfo) -> int:
        """Number of distinct values that would be processed for a detection."""
        if 'json_column' in info:
            parsed = JsonWalker.parse_json_documents(df, info['json_column'])
            return len(JsonWalker.json_column_values(parsed, info['json_path']))

        values = JsonWalker.leaf_values(df, info['column'])
        return 0 if values is None else len(values)

    def process(
        self,
        df: pl.DataFrame,
        pii_columns: Optional[List[PIIColumnInfo]] = None,
    ) -> pl.DataFrame:
        """Fake or redact the detected PII in `df` and record it in `self.mapper`."""
        if pii_columns is None:
            pii_columns = self.detect(df)

        json_groups: Dict[str, List[PIIColumnInfo]] = {}
        for info in pii_columns:
            if 'json_column' in info:
                json_groups.setdefault(info['json_column'], []).append(info)
            else:
                df = self._process_value_column(df, info)

        for column, group in json_groups.items():
            df = self._process_json_column(df, column, group)

        return df

    # ------------------------------------------------------------------

    def _process_value_column(self, df: pl.DataFrame, info: PIIColumnInfo) -> pl.DataFrame:
        key = info['column']

        values = JsonWalker.leaf_values(df, key)
        if values is None:
            self.skipped.append(key)
            return df

        field_name = JsonWalker.segments(key, df.schema)[-1]
        mapping = self._mapping_for(key, info['category'], field_name, values.to_list())
        if not mapping:
            return df

        if key in df.columns and df.schema[key] == pl.String:
            return df.with_columns(pl.col(key).replace(mapping))

        expr = JsonWalker.rewrite_expr(df.schema, key, mapping)
        return df if expr is None else df.with_columns(expr)

    def _process_json_column(
        self,
        df: pl.DataFrame,
        column: str,
        group: List[PIIColumnInfo],
    ) -> pl.DataFrame:
        parsed = JsonWalker.parse_json_documents(df, column)

        rules = []
        for info in group:
            path = info['json_path']
            values = JsonWalker.json_column_values(parsed, path)
            mapping = self._mapping_for(
                info['column'], info['category'], JsonWalker.segments(path)[-1], values
            )
            if mapping:
                rules.append((path, mapping))

        if not rules:
            return df
        return JsonWalker.rewrite_json_column(df, column, rules, parsed)

    def _mapping_for(
        self,
        key: str,
        category: str,
        field_name: str,
        values: List[str],
    ) -> Dict[str, str]:
        """Mapping for `key` covering `values`, reusing earlier assignments."""
        scope = category if self._generator is not None else None
        shared = self._shared.setdefault(scope, {})
        taken = self._shared_replacements.setdefault(scope, set())

        missing = [v for v in values if v not in shared]
        if missing:
            series = pl.Series(missing, dtype=pl.String)
            if self._generator is not None:
                created = self._generator.build_mapping(series, category, field_name, reserved=taken)
            else:
                created = self._redactor.build_mapping(series, field_name, reset_counter=False)
            shared.update(created)
            taken.update(created.values())

        known = self._known.setdefault(key, {})
        for value in values:
            known[value] = shared[value]

        if known:
            self.mapper.add_column_mapping(key, category, dict(known))
        return known
