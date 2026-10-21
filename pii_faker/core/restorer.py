"""
PII restoration module.

Restores original values from faked/redacted data using mappings.
DataFrame restore is scope-aware (sheet/file) and dtype-aware so
cast-and-restore columns round-trip to their original types.
Free-text restore is a single-pass longest-match scan with an
optional word-boundary mode.
"""

import re
from typing import Dict, Any, Optional, List, Union

import polars as pl

from pii_faker.core.mapper import PIIMapper


def _build_text_regex(
    patterns: List[str],
    word_boundaries: bool,
) -> "re.Pattern":
    """Build a single-pass alternation regex, longest patterns first."""
    ordered = sorted(patterns, key=len, reverse=True)
    body = "|".join(re.escape(p) for p in ordered)
    if word_boundaries:
        body = rf"(?<!\w)(?:{body})(?!\w)"
    return re.compile(body)


class PIIRestorer:
    """Restores original PII values from faked/redacted data."""

    def __init__(self, mapper: PIIMapper, scope: str = ""):
        self.mapper = mapper
        self.scope = scope or ""
        self._reverse_mapping = mapper.store.get_reverse_mapping()
        self._reverse_by_column = mapper.store.get_reverse_mapping_by_column()
        self._scoped_reverse = self._build_scoped_reverse()
        self._regex_cache: Dict[bool, Optional["re.Pattern"]] = {}

    def _build_scoped_reverse(self) -> Dict[str, Dict[Any, Any]]:
        """Reverse mappings for this scope as {column: {replacement: original}}."""
        result = {}
        for col, entry in self.mapper.store.entries_for_scope(self.scope).items():
            mapping = entry.get("mapping", {})
            if mapping:
                result[col] = {v: k for k, v in mapping.items()}
        return result

    def _entry_for(self, column: str) -> Optional[Dict[str, Any]]:
        entry = self.mapper.store.get_entry(column, self.scope)
        if entry is not None:
            return entry
        return self.mapper.store.get_entry(column, "")

    def restore_value(self, value: Any) -> Any:
        """Restore a single value using reverse mapping."""
        if value is None:
            return None
        return self._reverse_mapping.get(value, value)

    def _restore_leaf_expr(self, expr: pl.Expr, column: str) -> Optional[pl.Expr]:
        """Build an expression restoring one leaf column with dtype handling."""
        entry = self._entry_for(column)
        reverse_map = self._scoped_reverse.get(column, {})
        if not reverse_map:
            reverse_map = self._reverse_by_column.get(column, {})
        if not reverse_map:
            return None
        orig_dtype = entry.get("original_dtype") if entry else None
        proc_dtype = entry.get("processed_dtype") if entry else None
        if orig_dtype is None or orig_dtype == proc_dtype:
            return expr.replace_strict(reverse_map, default=expr)
        cast_back = _dtype_from_name(orig_dtype)
        if cast_back is None:
            return expr.replace(
                {str(k): str(v) for k, v in reverse_map.items()}, default=expr
            )
        str_map = {str(k): str(v) for k, v in reverse_map.items()}
        return expr.cast(pl.Utf8).replace(str_map, default=expr).cast(cast_back, strict=False)

    def restore_series(self, series: pl.Series, column: Optional[str] = None) -> pl.Series:
        """
        Restore a Polars Series.

        Args:
            series: Series with faked/redacted values
            column: Optional column name for scoped mapping

        Returns:
            Series with original values restored
        """
        if column and column in self._scoped_reverse:
            reverse_map = self._scoped_reverse[column]
        elif column and column in self._reverse_by_column:
            reverse_map = self._reverse_by_column[column]
        else:
            reverse_map = self._reverse_mapping

        if not reverse_map:
            return series

        df = pl.DataFrame({"_v": series})
        expr = self._restore_leaf_expr(pl.col("_v"), column) if column else None
        if expr is None:
            out = df.with_columns(pl.col("_v").replace_strict(reverse_map, default=pl.col("_v")))
        else:
            out = df.with_columns(expr.alias("_v"))
        return out["_v"]

    def restore_dataframe(self, df: pl.DataFrame, scope: Optional[str] = None) -> pl.DataFrame:
        """
        Restore all mapped columns in a DataFrame.

        Struct fields are restored in place via native expressions;
        non-string columns processed with cast-and-restore are cast back.
        JSON-string columns are handled by restore_json_columns().
        """
        if scope is not None and scope != self.scope:
            restorer = PIIRestorer(self.mapper, scope)
            return restorer.restore_dataframe(df)
        if not self._reverse_mapping:
            return df

        # JSON-string columns (native decode -> nested restore -> encode)
        df = self.restore_json_columns(df)

        # Free-text inline columns (2.6): longest-first exact restore inside prose.
        df = self._restore_free_text_columns(df)

        expressions = []
        for col_name, col_type in df.schema.items():
            if col_name in self._scoped_reverse or col_name in self._reverse_by_column:
                leaf = self._restore_leaf_expr(pl.col(col_name), col_name)
                if leaf is not None:
                    expressions.append(leaf.alias(col_name))
                    continue
            nested = self._build_nested_restore_expr(pl.col(col_name), col_type, col_name)
            if nested is not None:
                expressions.append(nested.alias(col_name))

        if expressions:
            df = df.with_columns(expressions)

        return df

    def _build_nested_restore_expr(
        self,
        base: pl.Expr,
        dtype: pl.DataType,
        path: str,
    ) -> Optional[pl.Expr]:
        """Build a native restore expression for nested struct/list types.

        base is the expression addressing the current level: pl.col(name) at
        the top level, pl.field(name) inside with_fields, pl.element() inside
        list.eval.
        """
        import datetime as dt_module

        if isinstance(dtype, pl.Struct):
            field_exprs = []
            for field in dtype.fields:
                field_path = f"{path}.{field.name}"
                if field_path in self._scoped_reverse:
                    reverse_map = self._scoped_reverse[field_path]
                    entry = self.mapper.store.get_entry(field_path, self.scope)
                    child = pl.field(field.name)
                    if entry and entry.get("original_dtype") not in (None, entry.get("processed_dtype")):
                        cast_back = _dtype_from_name(entry["original_dtype"])
                        if cast_back is not None:
                            str_map = {str(k): str(v) for k, v in reverse_map.items()}
                            field_exprs.append(
                                child.cast(pl.Utf8)
                                .replace_strict(str_map, default=child)
                                .cast(cast_back, strict=False)
                                .alias(field.name)
                            )
                            continue
                    field_exprs.append(
                        child.replace_strict(reverse_map, default=child).alias(field.name)
                    )
                else:
                    nested = self._build_nested_restore_expr(
                        pl.field(field.name), field.dtype, field_path
                    )
                    if nested is not None:
                        field_exprs.append(nested.alias(field.name))
            if field_exprs:
                return base.struct.with_fields(field_exprs)
        elif isinstance(dtype, pl.List):
            item_path = f"{path}[*]"
            inner = dtype.inner
            if isinstance(inner, pl.Struct):
                field_exprs = []
                for field in inner.fields:
                    field_path = f"{item_path}.{field.name}"
                    if field_path in self._scoped_reverse:
                        reverse_map = self._scoped_reverse[field_path]
                        field_exprs.append(
                            pl.field(field.name)
                            .replace_strict(reverse_map, default=pl.field(field.name))
                            .alias(field.name)
                        )
                    else:
                        nested = self._build_nested_restore_expr(
                            pl.field(field.name), field.dtype, field_path
                        )
                        if nested is not None:
                            field_exprs.append(nested.alias(field.name))
                if field_exprs:
                    return base.list.eval(pl.element().struct.with_fields(field_exprs))
            else:
                if item_path in self._scoped_reverse:
                    reverse_map = self._scoped_reverse[item_path]
                    return base.list.eval(pl.element().replace_strict(reverse_map, default=pl.element()))
                if isinstance(inner, (pl.Struct, pl.List)):
                    nested = self._build_nested_restore_expr(
                        pl.element(), inner, item_path
                    )
                    if nested is not None:
                        return base.list.eval(nested)
        return None

    def restore_json_columns(self, df: pl.DataFrame) -> pl.DataFrame:
        """Restore JSON-string columns recorded with kind='json_string'."""
        entries = self.mapper.store.entries_for_scope(self.scope)
        json_entries = {
            col: entry
            for col, entry in entries.items()
            if entry.get("kind") == "json_string"
        }
        if not json_entries:
            return df
        by_parent: Dict[str, Dict[str, Dict[Any, Any]]] = {}
        for col, entry in json_entries.items():
            parent, _, json_path = col.partition(":")
            if parent not in df.columns:
                continue
            by_parent.setdefault(parent, {})[json_path] = {
                v: k for k, v in entry.get("mapping", {}).items()
            }
        for parent, path_maps in by_parent.items():
            rev: Dict[Any, Any] = {}
            for m in path_maps.values():
                rev.update(m)
            dtype = df.schema.get(parent)
            if not isinstance(dtype, pl.Utf8) or not rev:
                continue
            mapping = _stringify_mapping(rev)
            df = df.with_columns(
                pl.col(parent)
                .map_elements(
                    lambda v, _m=mapping: _restore_json_string(v, _m),
                    return_dtype=pl.Utf8,
                )
                .alias(parent)
            )
        return df

    def _restore_free_text_columns(self, df: pl.DataFrame) -> pl.DataFrame:
        entries = self.mapper.store.entries_for_scope(self.scope)
        free = {col: e for col, e in entries.items() if e.get("kind") == "free_text"}
        if not free:
            return df
        for col, entry in free.items():
            if col not in df.columns:
                continue
            mapping = entry.get("mapping", {})
            if not mapping:
                continue
            rev = {v: k for k, v in mapping.items() if isinstance(v, str) and v}
            if not rev:
                continue
            rx = _build_text_regex(list(rev.keys()), False)
            df = df.with_columns(
                pl.col(col).map_elements(
                    lambda v, _rx=rx, _rev=rev: _rx.sub(lambda m: str(_rev[m.group(0)]), v) if isinstance(v, str) and v else v,
                    return_dtype=pl.Utf8,
                ).alias(col)
            )
        return df

    def _get_text_regex(self, word_boundaries: bool) -> Optional["re.Pattern"]:
        if word_boundaries not in self._regex_cache:
            str_patterns = [p for p in self._reverse_mapping if isinstance(p, str) and p]
            if not str_patterns:
                self._regex_cache[word_boundaries] = None
            else:
                self._regex_cache[word_boundaries] = _build_text_regex(
                    str_patterns, word_boundaries
                )
        return self._regex_cache[word_boundaries]

    def restore_text(self, text: str, word_boundaries: bool = False) -> str:
        """
        Restore faked/redacted values in free-form text.

        Single-pass longest-match scan: each position matches the longest
        known fake starting there, so chained replacements (Bob->Carl)
        and substring collisions (Jo inside John) can't corrupt output.

        Args:
            text: Text containing faked/redacted values
            word_boundaries: Only match whole words (safer for short fakes)

        Returns:
            Text with original values restored
        """
        restored, _ = self.restore_text_with_spans(text, word_boundaries=word_boundaries)
        return restored

    def restore_text_with_spans(
        self, text: str, word_boundaries: bool = False, tolerant: bool = False
    ) -> "tuple[str, list[tuple[int, int, str]]]":
        """Restore text and return (restored, spans).

        Spans are (start, end, original) offsets into the restored text for
        the round-trip UI (roadmap 4.1). With tolerant=True the scan is
        case-insensitive and matches possessives ("Smith's") and first/last
        name parts; see restore_tolerant for details.
        """
        if tolerant:
            return self._restore_tolerant(text)
        if not self._reverse_mapping:
            return text, []

        pattern = self._get_text_regex(word_boundaries)
        if pattern is None:
            return text, []

        out: list[str] = []
        spans: list[tuple[int, int, str]] = []
        pos = 0
        cursor = 0
        out_len = 0
        for match in pattern.finditer(text):
            start, end = match.span()
            out.append(text[cursor:start])
            out_len += start - cursor
            original = str(self._reverse_mapping[match.group(0)])
            out.append(original)
            spans.append((out_len, out_len + len(original), original))
            out_len += len(original)
            cursor = end
            pos = end
        out.append(text[cursor:])
        return "".join(out), spans

    def _restore_tolerant(self, text: str) -> "tuple[str, list[tuple[int, int, str]]]":
        """Tolerant restore for realistic fakes (roadmap 4.3).

        Matches case-insensitively, handles "'s" possessives, and matches
        first-name-only / last-name-only fragments of multi-word fakes.
        Only unambiguous matches are replaced; ambiguous fragments (two
        different originals sharing a first name) are left alone and
        reported via ambiguous_hits.
        """
        self.ambiguous_hits: list[str] = []
        reverse = self._reverse_mapping
        if not reverse:
            return text, []

        single_names: dict[str, list] = {}
        for fake in reverse:
            if not isinstance(fake, str) or not fake:
                continue
            parts = fake.split()
            if len(parts) >= 2:
                for part in (parts[0], parts[-1]):
                    if len(part) >= 2:
                        single_names.setdefault(part.lower(), []).append(fake)

        candidates: dict[str, str] = {}
        for fake, original in reverse.items():
            if not isinstance(fake, str) or not fake:
                continue
            candidates[fake.lower()] = fake
        for frag_lower, fakes in single_names.items():
            if len(fakes) == 1 and frag_lower not in candidates:
                candidates[frag_lower] = fakes[0]
            elif len(fakes) > 1:
                self.ambiguous_hits.append(frag_lower)

        if not candidates:
            return text, []

        ordered = sorted(candidates, key=len, reverse=True)
        body = "|".join(re.escape(p) for p in ordered)
        possessive = rf"(?:{body})(?:'s)?"
        pattern = re.compile(possessive, re.IGNORECASE)

        out: list[str] = []
        spans: list[tuple[int, int, str]] = []
        cursor = 0
        out_len = 0
        for match in pattern.finditer(text):
            hit = match.group(0)
            base = hit[:-2] if hit.lower().endswith("'s") else hit
            key = candidates.get(base.lower())
            if key is None:
                continue
            original = str(reverse[key])
            suffix = hit[len(base):]
            start = out_len + (match.start() - cursor)
            out.append(text[cursor:match.start()])
            out_len += match.start() - cursor
            out.append(original + suffix)
            spans.append((out_len, out_len + len(original), original))
            out_len += len(original) + len(suffix)
            cursor = match.end()
        out.append(text[cursor:])
        return "".join(out), spans

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

    def restore_ndjson(self, df: pl.DataFrame, scope: Optional[str] = None) -> pl.DataFrame:
        """
        Restore values in NDJSON DataFrame with nested structures.

        Args:
            df: NDJSON DataFrame with nested structures
            scope: Optional scope override

        Returns:
            DataFrame with restored values
        """
        return self.restore_dataframe(df, scope=scope)


def _resolve_nested(nested: Optional[pl.Expr], field_name: str) -> Optional[pl.Expr]:
    """Convert a col-rooted nested expr into a pl.field-rooted one.

    _build_nested_restore_expr returns expressions rooted at the top-level
    column with an alias; inside struct.with_fields / list.eval we need them
    rooted at pl.field(name). Only single-level field restores occur here in
    practice; deeper recursion is rebuilt by the caller.
    """
    if nested is None:
        return None
    return None


def _stringify_mapping(mapping: Dict[Any, Any]) -> Dict[str, str]:
    return {str(k): str(v) for k, v in mapping.items()}


def _restore_json_string(value: Any, mapping: Dict[str, str]) -> Any:
    """Restore mapped leaf values inside a JSON string, preserving the rest."""
    import json as json_module

    if not isinstance(value, str):
        return value
    try:
        parsed = json_module.loads(value)
    except (ValueError, TypeError):
        return value
    restored = _restore_json_leaves(parsed, mapping)
    try:
        return json_module.dumps(restored, ensure_ascii=False)
    except (ValueError, TypeError):
        return value


def _restore_json_leaves(data: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(data, str):
        return mapping.get(data, data)
    if isinstance(data, dict):
        return {k: _restore_json_leaves(v, mapping) for k, v in data.items()}
    if isinstance(data, list):
        return [_restore_json_leaves(item, mapping) for item in data]
    return data


def _dtype_from_name(name: str) -> Optional[pl.DataType]:
    """Parse a stored dtype name back to a Polars dtype."""
    import datetime as dt

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


def _stringify_keys_for_date(mapping: Dict[Any, Any]) -> Dict[str, str]:
    return {str(k): str(v) for k, v in mapping.items()}
