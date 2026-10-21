"""
JSON walker module for handling nested structures.

Provides functionality to traverse, transform, and restore nested JSON,
including handling of struct types, lists, and JSON paths with wildcards.
"""

import json
import re
import warnings
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, Union

import polars as pl


class JsonWalker:
    """Walks and transforms nested JSON structures."""

    def __init__(self, strict_paths: bool = False):
        """
        Args:
            strict_paths: If True, bare names only match top-level.
                         If False (default), bare names match at any depth.
        """
        self.strict_paths = strict_paths

    @staticmethod
    def parse_path(path: str) -> List[Tuple[str, bool]]:
        """
        Parse a dotted path into segments.

        Returns:
            List of (segment_name, is_wildcard) tuples
        """
        segments = []
        parts = path.split('.')

        for part in parts:
            # Check if this part contains [*]
            if '[*]' in part:
                # Remove [*] from the name and mark as wildcard
                name = part.replace('[*]', '')
                segments.append((name, True))
            else:
                segments.append((part, False))

        return segments

    @staticmethod
    def find_pii_paths_in_schema(
        schema: Dict[str, pl.DataType],
        pii_names: Set[str],
        parent_path: str = "",
        strict: bool = False,
    ) -> List[str]:
        """
        Find all paths in a schema that match PII names.

        Args:
            schema: Polars schema dict
            pii_names: Set of normalized PII names to match
            parent_path: Current path prefix
            strict: If True, only match top-level

        Returns:
            List of matching paths
        """
        matching_paths = []

        for field_name, field_type in schema.items():
            current_path = f"{parent_path}.{field_name}" if parent_path else field_name
            normalized_name = re.sub(r'[\s_\-]+', '', field_name.lower())

            # Check if this field matches PII
            if normalized_name in pii_names:
                matching_paths.append(current_path)

            # Recurse into nested types
            if not strict:
                if isinstance(field_type, pl.Struct):
                    nested_schema = {f.name: f.dtype for f in field_type.fields}
                    matching_paths.extend(
                        JsonWalker.find_pii_paths_in_schema(
                            nested_schema, pii_names, current_path, strict
                        )
                    )
                elif isinstance(field_type, pl.List) and isinstance(field_type.inner, pl.Struct):
                    nested_schema = {f.name: f.dtype for f in field_type.inner.fields}
                    matching_paths.extend(
                        JsonWalker.find_pii_paths_in_schema(
                            nested_schema, pii_names, f"{current_path}[*]", strict
                        )
                    )

        return matching_paths

    @staticmethod
    def apply_to_nested(
        data: Any,
        pathsegments: List[Tuple[str, bool]],
        transform_fn: callable,
        current_index: int = 0,
    ) -> Any:
        """
        Apply a transformation function to nested data at a specific path.

        Args:
            data: The data structure (dict, list, or primitive)
            pathsegments: Parsed path segments
            transform_fn: Function to apply to matched values
            current_index: Current position in pathsegments

        Returns:
            Transformed data
        """
        if current_index >= len(pathsegments):
            return transform_fn(data) if isinstance(data, str) else data

        segment_name, is_wildcard = pathsegments[current_index]

        if isinstance(data, dict):
            if segment_name in data:
                if is_wildcard and isinstance(data[segment_name], list):
                    # Process each element in the array
                    data[segment_name] = [
                        JsonWalker.apply_to_nested(
                            item, pathsegments, transform_fn, current_index + 1
                        )
                        for item in data[segment_name]
                    ]
                else:
                    data[segment_name] = JsonWalker.apply_to_nested(
                        data[segment_name], pathsegments, transform_fn, current_index + 1
                    )
            # Also recurse into all values for non-strict matching
            else:
                for key in data:
                    data[key] = JsonWalker.apply_to_nested(
                        data[key], pathsegments, transform_fn, current_index
                    )

        elif isinstance(data, list):
            if is_wildcard:
                data = [
                    JsonWalker.apply_to_nested(
                        item, pathsegments, transform_fn, current_index + 1
                    )
                    for item in data
                ]
            else:
                data = [
                    JsonWalker.apply_to_nested(
                        item, pathsegments, transform_fn, current_index
                    )
                    for item in data
                ]

        return data

    @staticmethod
    def find_leaf_values(
        data: Any,
        target_key: str,
        path: str = "",
        strict: bool = False,
    ) -> List[Tuple[str, str]]:
        """
        Find all leaf values matching a key name in nested structure.

        Args:
            data: The data structure
            target_key: Key name to find (normalized)
            path: Current path
            strict: If True, only match at current level

        Returns:
            List of (path, value) tuples
        """
        results = []
        normalized_target = re.sub(r'[\s_\-]+', '', target_key.lower())

        if isinstance(data, dict):
            for key, value in data.items():
                current_path = f"{path}.{key}" if path else key
                normalized_key = re.sub(r'[\s_\-]+', '', key.lower())

                if normalized_key == normalized_target and isinstance(value, str):
                    results.append((current_path, value))
                elif not strict:
                    results.extend(
                        JsonWalker.find_leaf_values(value, target_key, current_path, strict)
                    )

        elif isinstance(data, list):
            for i, item in enumerate(data):
                item_path = f"{path}[{i}]" if path else f"[{i}]"
                results.extend(
                    JsonWalker.find_leaf_values(item, target_key, item_path, strict)
                )

        return results

    @staticmethod
    def collect_unique_string_values(
        data: Any,
        path: str = "",
    ) -> Dict[str, Set[str]]:
        """
        Collect all unique string values organized by path.

        Returns:
            Dict mapping path -> set of unique string values
        """
        result: Dict[str, Set[str]] = {}

        if isinstance(data, str):
            if path:
                result.setdefault(path, set()).add(data)
        elif isinstance(data, dict):
            for key, value in data.items():
                current_path = f"{path}.{key}" if path else key
                nested = JsonWalker.collect_unique_string_values(value, current_path)
                for k, v in nested.items():
                    result.setdefault(k, set()).update(v)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                item_path = f"{path}[*]" if path else "[*]"
                nested = JsonWalker.collect_unique_string_values(item, item_path)
                for k, v in nested.items():
                    result.setdefault(k, set()).update(v)

        return result

    # ------------------------------------------------------------------
    # Native nested columns (Struct / List dtypes), vectorised
    # ------------------------------------------------------------------

    @staticmethod
    def segments(path: str, columns: Optional[Any] = None) -> List[str]:
        """
        Field names along a path. Lists are traversed transparently, so
        `a.b[*].c` and `a.b.c` address the same field.

        A path that is exactly a top-level column name (`columns`) is one
        segment, so flat columns whose names contain dots or brackets work.
        """
        if columns is not None and path in columns:
            return [path]
        return [name for name, _ in JsonWalker.parse_path(path) if name]

    @staticmethod
    def _text_dtype(dtype: pl.DataType) -> bool:
        """True for String, or lists of String (nested to any depth)."""
        while isinstance(dtype, pl.List):
            dtype = dtype.inner
        return dtype == pl.String

    @staticmethod
    def leaf_values(df: pl.DataFrame, path: str) -> Optional[pl.Series]:
        """
        Unique non-null strings found at `path` in a DataFrame.

        Works for flat columns, structs and lists of structs. Returns None when
        the path doesn't exist or doesn't end in text.
        """
        segs = JsonWalker.segments(path, df.schema)
        if not segs or segs[0] not in df.schema:
            return None

        with warnings.catch_warnings():
            # explode()'s empty-list default is changing; we drop nulls anyway.
            warnings.simplefilter("ignore", DeprecationWarning)

            expr: pl.Expr = pl.col(segs[0])
            dtype = df.schema[segs[0]]

            for name in segs[1:]:
                while isinstance(dtype, pl.List):
                    expr, dtype = expr.explode(), dtype.inner
                if not isinstance(dtype, pl.Struct):
                    return None
                fields = {f.name: f.dtype for f in dtype.fields}
                if name not in fields:
                    return None
                expr, dtype = expr.struct.field(name), fields[name]

            while isinstance(dtype, pl.List):
                expr, dtype = expr.explode(), dtype.inner
            if dtype != pl.String:
                return None

            values = df.select(expr.alias("v")).to_series()
        return values.drop_nulls().unique()

    @staticmethod
    def rewrite_expr(
        schema: Dict[str, pl.DataType],
        path: str,
        mapping: Dict[str, str],
    ) -> Optional[pl.Expr]:
        """
        Build an expression that replaces whole string values at `path`.

        The result is aliased to the top-level column, so it can be passed
        straight to `with_columns`. Returns None if the path doesn't lead to
        text. Nulls, empty lists and the rest of the structure are untouched.
        """
        segs = JsonWalker.segments(path, schema)
        if not segs or segs[0] not in schema:
            return None

        def rewrite(expr: pl.Expr, dtype: pl.DataType, i: int) -> Optional[pl.Expr]:
            if isinstance(dtype, pl.List):
                inner = rewrite(pl.element(), dtype.inner, i)
                return None if inner is None else expr.list.eval(inner)
            if i == len(segs):
                return expr.replace(mapping) if dtype == pl.String else None
            if not isinstance(dtype, pl.Struct):
                return None
            fields = {f.name: f.dtype for f in dtype.fields}
            if segs[i] not in fields:
                return None
            child = rewrite(pl.field(segs[i]), fields[segs[i]], i + 1)
            return None if child is None else expr.struct.with_fields(child)

        result = rewrite(pl.col(segs[0]), schema[segs[0]], 1)
        return None if result is None else result.alias(segs[0])

    # ------------------------------------------------------------------
    # JSON documents stored as strings in a cell
    # ------------------------------------------------------------------

    @staticmethod
    def json_values(doc: Any, segs: List[str]) -> Iterator[str]:
        """Yield every string at the path `segs` inside a parsed JSON document."""
        if isinstance(doc, list):
            for item in doc:
                yield from JsonWalker.json_values(item, segs)
        elif not segs:
            if isinstance(doc, str):
                yield doc
        elif isinstance(doc, dict) and segs[0] in doc:
            yield from JsonWalker.json_values(doc[segs[0]], segs[1:])

    @staticmethod
    def map_json_values(doc: Any, segs: List[str], fn: Callable[[str], str]) -> Any:
        """Apply `fn` to every string at the path `segs`, in place where possible."""
        if isinstance(doc, list):
            return [JsonWalker.map_json_values(item, segs, fn) for item in doc]
        if not segs:
            return fn(doc) if isinstance(doc, str) else doc
        if isinstance(doc, dict) and segs[0] in doc:
            doc[segs[0]] = JsonWalker.map_json_values(doc[segs[0]], segs[1:], fn)
        return doc

    @staticmethod
    def parse_json_documents(df: pl.DataFrame, column: str) -> Dict[str, Any]:
        """Parse each distinct JSON string in a column once; skips invalid ones."""
        parsed: Dict[str, Any] = {}
        for text in df[column].drop_nulls().unique().to_list():
            try:
                parsed[text] = json.loads(text)
            except ValueError:
                continue
        return parsed

    @staticmethod
    def json_column_values(
        parsed: Dict[str, Any],
        path: str,
    ) -> List[str]:
        """Sorted distinct strings at `path` across parsed documents."""
        segs = JsonWalker.segments(path)
        return sorted({v for doc in parsed.values() for v in JsonWalker.json_values(doc, segs)})

    @staticmethod
    def rewrite_json_column(
        df: pl.DataFrame,
        column: str,
        rules: List[Tuple[str, Dict[str, str]]],
        parsed: Optional[Dict[str, Any]] = None,
    ) -> pl.DataFrame:
        """
        Replace strings inside the JSON documents of a text column.

        Args:
            rules: (path, {old: new}) pairs applied to every document.
            parsed: Optional output of parse_json_documents, to avoid re-parsing.

        Documents that change are re-serialised compactly; the rest keep their
        original text byte for byte.
        """
        if parsed is None:
            parsed = JsonWalker.parse_json_documents(df, column)

        rewritten: Dict[str, str] = {}
        for text, doc in parsed.items():
            changed = False

            for path, mapping in rules:
                def swap(value: str, mapping: Dict[str, str] = mapping) -> str:
                    nonlocal changed
                    new = mapping.get(value, value)
                    changed = changed or new != value
                    return new

                doc = JsonWalker.map_json_values(doc, JsonWalker.segments(path), swap)

            if changed:
                rewritten[text] = json.dumps(doc, separators=(',', ':'), ensure_ascii=False)

        if not rewritten:
            return df
        return df.with_columns(pl.col(column).replace(rewritten))
