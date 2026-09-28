"""
JSON walker module for handling nested structures.

Provides functionality to traverse, transform, and restore nested JSON,
including handling of struct types, lists, and JSON paths with wildcards.
"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple, Union

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
        path_segments: List[Tuple[str, bool]],
        transform_fn: callable,
        current_index: int = 0,
    ) -> Any:
        """
        Apply a transformation function to nested data at a specific path.

        Args:
            data: The data structure (dict, list, or primitive)
            path_segments: Parsed path segments
            transform_fn: Function to apply to matched values
            current_index: Current position in path_segments

        Returns:
            Transformed data
        """
        if current_index >= len(path_segments):
            return transform_fn(data) if isinstance(data, str) else data

        segment_name, is_wildcard = path_segments[current_index]

        if isinstance(data, dict):
            if segment_name in data:
                if is_wildcard and isinstance(data[segment_name], list):
                    # Process each element in the array
                    data[segment_name] = [
                        JsonWalker.apply_to_nested(
                            item, path_segments, transform_fn, current_index + 1
                        )
                        for item in data[segment_name]
                    ]
                else:
                    data[segment_name] = JsonWalker.apply_to_nested(
                        data[segment_name], path_segments, transform_fn, current_index + 1
                    )
            # Also recurse into all values for non-strict matching
            else:
                for key in data:
                    data[key] = JsonWalker.apply_to_nested(
                        data[key], path_segments, transform_fn, current_index
                    )

        elif isinstance(data, list):
            if is_wildcard:
                data = [
                    JsonWalker.apply_to_nested(
                        item, path_segments, transform_fn, current_index + 1
                    )
                    for item in data
                ]
            else:
                data = [
                    JsonWalker.apply_to_nested(
                        item, path_segments, transform_fn, current_index
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
