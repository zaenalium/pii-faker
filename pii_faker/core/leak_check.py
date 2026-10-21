"""
Leak check: re-scan processed output for residual PII (roadmap 2.3).

Two complementary scans run over the OUTPUT frame:

1. Value-shape re-scan — the 2.1 detectors run over every column. Hits on
   non-targeted columns (or targeted columns whose values survived) are
   reported. Columns the run intentionally processed are expected to contain
   *fake-shaped* values (fake emails still look like emails), so a hit on a
   processed column is only a leak when the ORIGINAL value is still present.
2. Original-value scan — every original value recorded in the mapping is
   searched for in the output. Any exact hit is a definite leak (missed
   column, nested path, or JSON-string leaf).

The check returns a LeakReport (no PII values included — only counts, paths
and detector names, so the report itself is safe to log).
"""

import warnings
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Optional, Set

import polars as pl

from pii_faker.core.value_detectors import scan_frame_values


@dataclass
class LeakFinding:
    """One residual-PII finding (no raw values — safe to log)."""

    kind: str  # "original_value" | "value_shape"
    path: str
    detail: str
    count: int = 0


@dataclass
class LeakReport:
    """Result of scanning processed output for residual PII."""

    clean: bool = True
    findings: List[LeakFinding] = dataclass_field(default_factory=list)

    @property
    def leak_count(self) -> int:
        return len(self.findings)

    def summary(self) -> str:
        if self.clean:
            return "leak check: clean (no residual PII found)"
        lines = [f"leak check: {len(self.findings)} finding(s)"]
        for finding in self.findings:
            lines.append(f"  - [{finding.kind}] {finding.path}: {finding.detail}")
        return "\n".join(lines)


def _iter_string_leaves(df: pl.DataFrame, prefix: str = ""):
    """Yield (path, Series) for every string leaf incl. nested structs/lists."""
    for col_name, dtype in df.schema.items():
        path = f"{prefix}.{col_name}" if prefix else col_name
        if isinstance(dtype, pl.Utf8):
            yield path, df[col_name]
        elif isinstance(dtype, pl.Struct):
            for f in dtype.fields:
                sub = df.select(pl.col(col_name).struct.field(f.name).alias("__v"))
                yield from _iter_string_leaves(
                    sub.rename({"__v": f.name}), path
                )
        elif isinstance(dtype, pl.List) and isinstance(dtype.inner, pl.Struct):
            # A list of structs: descend into the fields, as for a plain struct. Treating
            # the elements as text (below) compared whole structs, so an original inside
            # one could never be an exact hit and a processed list was flagged by shape.
            try:
                with warnings.catch_warnings():
                    # explode()'s empty-list default is changing; null rows are dropped anyway.
                    warnings.simplefilter("ignore", DeprecationWarning)
                    elements = df.select(pl.col(col_name).explode().alias("__v"))
            except Exception:
                continue
            for f in dtype.inner.fields:
                sub = elements.select(pl.col("__v").struct.field(f.name).alias(f.name))
                yield from _iter_string_leaves(sub, f"{path}[*]")
        elif isinstance(dtype, pl.List):
            try:
                exploded = df.select(
                    pl.col(col_name).list.eval(pl.element().cast(pl.Utf8)).alias("__v")
                )["__v"].explode().drop_nulls()
                yield f"{path}[*]", exploded
            except Exception:
                continue


def check_processed_frame(
    df: pl.DataFrame,
    originals: Set[str],
    processed_paths: Optional[Set[str]] = None,
    min_ratio: float = 0.3,
) -> LeakReport:
    """
    Scan a processed frame for residual PII.

    Args:
        df: Processed output frame
        originals: Original string values from the run's mappings
        processed_paths: Paths the run intentionally processed (value-shape
            hits here are expected — fakes keep realistic shapes — unless an
            original value is also present)
        min_ratio: Value-detector firing threshold

    Returns:
        LeakReport (contains no PII values)
    """
    report = LeakReport()
    processed_paths = processed_paths or set()
    wanted = {o for o in originals if isinstance(o, str) and o}
    if not wanted and not min_ratio:
        return report

    for path, series in _iter_string_leaves(df):
        values = series.drop_nulls().to_list()
        if not values:
            continue
        value_set = set(values)

        # 1. Original-value scan: definite leak on any exact hit.
        hits = wanted & value_set
        if hits:
            report.clean = False
            report.findings.append(LeakFinding(
                kind="original_value",
                path=path,
                detail=f"{len(hits)} original value(s) still present",
                count=len(hits),
            ))

        # 2. Value-shape scan on columns the run did NOT process.
        if path not in processed_paths and f"{path}[*]" not in processed_paths:
            from pii_faker.core.value_detectors import detect_value_shapes

            matches = detect_value_shapes(series, min_ratio)
            for match in matches:
                report.clean = False
                report.findings.append(LeakFinding(
                    kind="value_shape",
                    path=path,
                    detail=(
                        f"{match['detector']} shape at "
                        f"{match['ratio']:.0%} of sampled values "
                        f"({match['description']})"
                    ),
                    count=0,
                ))

    return report


def check_processed_sheets(
    sheets: Dict[str, pl.DataFrame],
    sheets_originals: Optional[Dict[str, Set[str]]] = None,
    sheets_processed: Optional[Dict[str, Set[str]]] = None,
    global_originals: Optional[Set[str]] = None,
    min_ratio: float = 0.3,
) -> Dict[str, LeakReport]:
    """Run check_processed_frame per sheet; originals default to global set."""
    reports: Dict[str, LeakReport] = {}
    for sheet_name, frame in sheets.items():
        if sheets_originals and sheet_name in sheets_originals:
            originals = sheets_originals[sheet_name]
        else:
            originals = global_originals or set()
        processed = (sheets_processed or {}).get(sheet_name, set())
        reports[sheet_name] = check_processed_frame(
            frame, originals, processed, min_ratio
        )
    return reports


def collect_run_originals(mapper) -> Set[str]:
    """All original string values recorded in a mapper."""
    originals: Set[str] = set()
    for entry in mapper.store._entries.values():
        for original in entry.get("mapping", {}):
            if isinstance(original, str) and original:
                originals.add(original)
    return originals


def collect_processed_paths(mapper, scope: str = "") -> Set[str]:
    """Top-level columns/paths recorded for a scope (flat + nested tops)."""
    paths: Set[str] = set()
    for col, entry in mapper.store.entries_for_scope(scope).items():
        kind = entry.get("kind", "flat")
        if kind == "json_string":
            parent = col.split(":")[0]
            paths.add(parent)
        elif kind == "nested":
            top = entry.get("path", col).split(".")[0].replace("[*]", "")
            paths.add(top)
            paths.add(entry.get("path", col))
        else:
            paths.add(col)
    return paths


def check_sheets_with_mapper(
    sheets: Dict[str, pl.DataFrame],
    mapper,
    single_scope: bool = False,
    min_ratio: float = 0.3,
) -> Dict[str, LeakReport]:
    """Leak-check processed sheets against a run mapper (scope-aware)."""
    global_originals = collect_run_originals(mapper)
    reports: Dict[str, LeakReport] = {}
    for sheet_name, frame in sheets.items():
        scope = "" if single_scope else sheet_name
        entries = mapper.store.entries_for_scope(scope)
        if not entries and not single_scope:
            entries = mapper.store.entries_for_scope("")
        originals: Set[str] = set()
        for entry in entries.values():
            for original in entry.get("mapping", {}):
                if isinstance(original, str) and original:
                    originals.add(original)
        processed = collect_processed_paths(mapper, scope)
        if not entries:
            processed = set()
            originals = global_originals
        reports[sheet_name] = check_processed_frame(
            frame, originals or global_originals, processed, min_ratio
        )
    return reports
