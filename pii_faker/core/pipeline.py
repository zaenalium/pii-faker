"""
Single processing pipeline for PII Faker (plan -> apply -> mapping).

This is the only code path that transforms data. The CLI, the web UI and
the Python API all call into it, so detection, nested handling and
JSON-string columns behave identically everywhere.

Targets:
- flat columns: top-level Series replaced natively (with cast-and-restore
  for non-string dtypes).
- nested paths: struct / list-of-struct leaves addressed via native
  expressions (struct.with_fields, list.eval, pl.field).
- json_string columns: JSON text decoded in Python, leaves mapped, then
  re-encoded; recorded as kind="json_string" so restore works.

Mapping keys are (scope, column-or-path); one RunState is shared across all
targets and scopes in a run so fakes stay unique and the same original gets
the same fake everywhere.
"""

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Dict, List, Optional

import datetime
import re

import polars as pl

from pii_faker.core.config import PIIConfig, PIIMode
from pii_faker.core.detector import PIIColumnDetector
from pii_faker.core.generators import PIIGenerator
from pii_faker.core.json_walker import JsonWalker
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.redactor import PIIRedactor
from pii_faker.core.state import RunState
from pii_faker.core import values as V


@dataclass
class Target:
    """One leaf to process: a flat column, a nested path, or a JSON-string leaf."""

    kind: str  # "flat" | "nested" | "json_string" | "free_text"
    column: str  # top-level column name
    path: str  # full dotted path (flat: same as column)
    category: str
    pattern: str = ""
    description: str = ""
    json_path: str = ""  # for json_string: dotted path inside the JSON text
    dtype_name: Optional[str] = None
    confidence: float = 1.0
    name_match: bool = True
    value_detector: Optional[str] = None
    value_ratio: float = 0.0


@dataclass
class PlanResult:
    targets: List[Target] = dataclass_field(default_factory=list)
    skipped: List[Dict[str, Any]] = dataclass_field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.targets)


def _resolve_extra_paths(
    df: pl.DataFrame,
    extra_columns: List[str],
    detector: PIIColumnDetector,
) -> List[Target]:
    """Turn dotted extra_pii_columns entries into nested/json targets if present."""
    targets: List[Target] = []
    for raw in extra_columns or []:
        raw = raw.strip()
        if not raw or "." not in raw and "[*]" not in raw:
            continue
        segments = [s for s in raw.split(".") if s]
        if not segments:
            continue
        top = segments[0].replace("[*]", "")
        if top not in df.columns:
            continue
        dtype = df.schema.get(top)
        if isinstance(dtype, (pl.Struct, pl.List)):
            resolved = _resolve_nested_segments(df, top, dtype, segments[1:], detector)
            targets.extend(resolved)
    return targets


def _resolve_nested_segments(
    df: pl.DataFrame,
    top: str,
    dtype: pl.DataType,
    segments: List[str],
    detector: PIIColumnDetector,
) -> List[Target]:
    """Walk struct/list dtype following path segments; emit a target if found."""
    current = dtype
    walked: List[str] = [top]
    for i, seg in enumerate(segments):
        clean = seg.replace("[*]", "")
        want_list = "[*]" in seg
        if isinstance(current, pl.Struct):
            names = [f.name for f in current.fields]
            if clean not in names:
                return []
            current = next(f.dtype for f in current.fields if f.name == clean)
            walked.append(clean + ("[*]" if want_list else ""))
        elif isinstance(current, pl.List) and isinstance(current.inner, pl.Struct):
            names = [f.name for f in current.inner.fields]
            if clean not in names:
                return []
            current = next(f.dtype for f in current.inner.fields if f.name == clean)
            prefix = walked[-1] if walked else top
            if "[*]" not in prefix:
                walked[-1] = prefix + "[*]"
            walked.append(clean + ("[*]" if want_list else ""))
        else:
            return []
        _ = i
    leaf_name = segments[-1].replace("[*]", "")
    match = detector._match_column(leaf_name)
    category = match["category"] if match else "custom"
    full_path = ".".join(walked)
    return [Target(
        kind="nested",
        column=top,
        path=full_path,
        category=category,
        pattern=match["original"] if match else leaf_name,
        description=(match.get("description", "") if match else f"User-defined: {full_path}"),
        dtype_name=str(current),
    )]


def plan_dataframe(
    df: pl.DataFrame,
    config: PIIConfig,
    selected: Optional[List[str]] = None,
) -> PlanResult:
    """
    Decide what to process in a DataFrame.

    Args:
        df: Input DataFrame
        config: PII configuration
        selected: Optional explicit paths (CLI auto-detect = None; web passes
            the user's selection). Paths use dotted notation and
            ``parent:json.path`` for JSON-string leaves.

    Returns:
        PlanResult with targets and skipped entries.
    """
    detector = PIIColumnDetector(config)
    plan = PlanResult()

    if selected is not None:
        wanted = [s.strip() for s in selected if s and s.strip()]
        # `a.b.c` and `a.b[*].c` name the same field: the web UI offers the first form
        # while nested targets carry the second, so accept either.
        selected_set = set(wanted) | {s.replace("[*]", "") for s in wanted}
        has_explicit_nested = any("." in s or "[*]" in s or ":" in s for s in wanted)
    else:
        wanted = []
        selected_set = set()
        has_explicit_nested = False

    flat_infos = {c["column"]: c for c in detector.detect(df)}
    value_infos = {
        c["column"]: c
        for c in detector.detect_with_confidence(df)
        if not c["name_match"]
    }
    nested_infos = {
        p["path"]: p
        for p in detector.detect_nested_paths(
            dict(df.schema),
            json_columns=config.json_columns,
        )
    }

    def _in_selected(path: str) -> bool:
        return path in selected_set or path.replace("[*]", "") in selected_set

    def _selected(path: str, column: str) -> bool:
        if selected is None:
            return True
        return _in_selected(path) or column in selected_set

    # Columns handled as JSON documents are processed leaf by leaf (below), never as one
    # flat value: value detection sees the email inside the text and would replace the
    # whole document with a single fake email.
    json_handled = set(config.json_columns or []) | {w.partition(":")[0] for w in wanted if ":" in w}

    # Flat columns (skip struct/list tops; those are covered by nested paths)
    for col_name, dtype in df.schema.items():
        if isinstance(dtype, (pl.Struct, pl.List)) or col_name in json_handled:
            continue
        info = flat_infos.get(col_name)
        value_info = value_infos.get(col_name)
        if info is None and value_info is None:
            continue
        if not _selected(col_name, col_name):
            continue
        if info is not None:
            category = info["category"]
            pattern = info.get("pattern", "")
            description = info.get("description", "")
            confidence, name_match = 1.0, True
            value_detector, value_ratio = None, 0.0
        else:
            category = value_info["category"]
            pattern = value_info.get("pattern", "")
            description = value_info.get("description", "")
            confidence = value_info.get("confidence", 0.5)
            name_match = False
            value_detector = value_info.get("value_detector")
            value_ratio = value_info.get("value_ratio", 0.0)
        if V.is_supported_leaf(dtype):
            plan.targets.append(Target(
                kind="flat",
                column=col_name,
                path=col_name,
                category=category,
                pattern=pattern,
                description=description,
                dtype_name=V.dtype_name(dtype),
                confidence=confidence,
                name_match=name_match,
                value_detector=value_detector,
                value_ratio=value_ratio,
            ))
        else:
            plan.skipped.append({
                "column": col_name,
                "path": col_name,
                "reason": f"unsupported dtype {dtype}; left unchanged",
                "category": category,
            })

    # Nested struct/list paths
    for path, info in nested_infos.items():
        top = path.split(".")[0].replace("[*]", "")
        if top not in df.columns:
            continue
        if selected is not None and not has_explicit_nested:
            continue
        if selected is not None and not _in_selected(path) and top not in selected_set:
            continue
        if config.strict_paths and "." in path and not path.startswith(top + "."):
            continue
        if any(t.path == path for t in plan.targets):
            continue
        plan.targets.append(Target(
            kind="nested",
            column=top,
            path=path,
            category=info["category"],
            pattern=info.get("pattern", ""),
            description=info.get("description", ""),
            dtype_name=str(df.schema[top]),
        ))

    # Explicit dotted extras that detection didn't already cover
    if selected is None or has_explicit_nested:
        extra_targets = _resolve_extra_paths(
            df, list(config.extra_pii_columns or []), detector
        )
        for target in extra_targets:
            if selected is not None and not _in_selected(target.path):
                continue
            if any(t.path == target.path for t in plan.targets):
                continue
            plan.targets.append(target)

    # JSON-string columns: from --json-columns or selected parent:json.path
    json_parents = list(config.json_columns or [])
    json_selected: Dict[str, List[str]] = {}
    if selected is not None:
        for item in wanted:
            if ":" in item:
                parent, _, jpath = item.partition(":")
                if parent in df.columns:
                    json_selected.setdefault(parent, []).append(jpath)
                    if parent not in json_parents:
                        json_parents.append(parent)
    for parent in json_parents:
        if parent not in df.columns:
            continue
        if selected is not None and parent not in selected_set and parent not in json_selected:
            continue
        for jpath, category, leaf in _discover_json_leaves(
            df, parent, detector, json_selected.get(parent)
        ):
            full = f"{parent}:{jpath}"
            if selected is not None and not _in_selected(full) and parent not in selected_set:
                if not json_selected.get(parent):
                    continue
            if any(t.path == full for t in plan.targets):
                continue
            plan.targets.append(Target(
                kind="json_string",
                column=parent,
                path=full,
                 category=category,
                pattern=leaf,
                description=f"JSON-string leaf: {full}",
                json_path=jpath,
            ))

    # Free-text NER inside text columns (2.6, opt-in via --ner so non-NER prose stays tooled separately)
    if getattr(config, "ner", False):
        try:
            from pii_faker.core.ner import column_looks_like_prose, extract_spans

            sampled_flat = {t.path for t in plan.targets if t.kind in ("flat", "free_text")}
            for col_name, dtype in df.schema.items():
                if col_name in sampled_flat:
                    continue
                if not V.is_string_dtype(dtype):
                    continue
                sample = [v for v in df[col_name].drop_nulls().head(20).to_list() if isinstance(v, str) and v]
                if not sample or not column_looks_like_prose(sample):
                    continue
                if not _selected(col_name, col_name):
                    continue
                has_pii = False
                for text in sample[:5]:
                    try:
                        if extract_spans(text):
                            has_pii = True
                            break
                    except ImportError:
                        has_pii = False
                        break
                if has_pii:
                    plan.targets.append(Target(
                        kind="free_text", column=col_name, path=col_name,
                        category="name", pattern="ner",
                        description="NER: names/addresses inside prose",
                        dtype_name=str(dtype),
                        confidence=0.6, name_match=False, value_detector="ner",
                    ))
        except ImportError:
            pass

    return plan


def _discover_json_leaves(
    df: pl.DataFrame,
    parent: str,
    detector: PIIColumnDetector,
    only_paths: Optional[List[str]] = None,
) -> List[Any]:
    """Sample a JSON-string column and find PII leaf paths."""
    import json as json_module

    sample = df[parent].drop_nulls().head(20).to_list()
    parsed_samples = []
    for val in sample:
        if isinstance(val, str):
            try:
                parsed = json_module.loads(val)
                parsed_samples.append(parsed)
            except (ValueError, TypeError):
                continue
    if not parsed_samples:
        return []
    found: Dict[str, Any] = {}
    for parsed in parsed_samples:
        for jpath, value in _walk_json_leaves(parsed):
            if not isinstance(value, str) or not value:
                continue
            leaf = jpath.split(".")[-1].replace("[*]", "")
            if only_paths and jpath not in only_paths and leaf not in only_paths:
                continue
            match = detector._match_column(leaf)
            if match is None and only_paths is None:
                continue
            category = match["category"] if match else "custom"
            if jpath not in found:
                found[jpath] = (jpath, category, leaf)
    if only_paths:
        for jpath in only_paths:
            if jpath not in found:
                leaf = jpath.split(".")[-1].replace("[*]", "")
                match = detector._match_column(leaf)
                found[jpath] = (jpath, match["category"] if match else "custom", leaf)
    return list(found.values())


def _walk_json_leaves(data: Any, prefix: str = ""):
    """Yield (dotted_path, str_value) for string leaves in parsed JSON."""
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            yield from _walk_json_leaves(value, path)
    elif isinstance(data, list):
        for item in data:
            yield from _walk_json_leaves(item, (prefix + "[*]") if prefix else "[*]")
    elif isinstance(data, str):
        yield (prefix, data)


def _collect_leaf_strings(df: pl.DataFrame, target: Target) -> List[str]:
    """Collect unique non-null string values at a target path."""
    try:
        series = _select_path_series(df, target)
    except Exception:
        return []
    if series is None:
        return []
    return sorted({v for v in series.drop_nulls().to_list() if isinstance(v, str) and v})


def _select_path_series(df: pl.DataFrame, target: Target) -> Optional[pl.Series]:
    """Select the values at a nested path as a Series (collect step)."""
    if target.kind == "flat":
        return df[target.column]
    segments = [s for s in target.path.split(".") if s]
    top = segments[0].replace("[*]", "")
    expr: Optional[pl.Expr] = None
    dtype = df.schema.get(top)
    if dtype is None:
        return None
    current: Any = pl.col(top)
    cur_dtype: Any = dtype
    for seg in segments[1:]:
        clean = seg.replace("[*]", "")
        want_list = "[*]" in seg
        if isinstance(cur_dtype, pl.Struct):
            current = current.struct.field(clean)
            cur_dtype = next(
                (f.dtype for f in cur_dtype.fields if f.name == clean), None
            )
            if cur_dtype is None:
                return None
        elif isinstance(cur_dtype, pl.List) and isinstance(cur_dtype.inner, pl.Struct):
            current = current.list.eval(pl.element().struct.field(clean)).explode()
            cur_dtype = next(
                (f.dtype for f in cur_dtype.inner.fields if f.name == clean), None
            )
            if cur_dtype is None:
                return None
        elif isinstance(cur_dtype, pl.List):
            current = current.explode()
            cur_dtype = cur_dtype.inner
        else:
            return None
        _ = want_list
    frame = df.select(current.alias("__v"))
    return frame["__v"]


def _build_nested_apply_expr(
    base: pl.Expr,
    dtype: pl.DataType,
    path: str,
    per_path: Dict[str, Dict[Any, Any]],
) -> Optional[pl.Expr]:
    """Build a native apply expression mirroring the restorer's nested builder."""
    if isinstance(dtype, pl.Struct):
        field_exprs = []
        for f in dtype.fields:
            field_path = f"{path}.{f.name}"
            if field_path in per_path:
                mapping = per_path[field_path]
                child = pl.field(f.name)
                field_exprs.append(
                    V.apply_mapping_expr(child, mapping).alias(f.name)
                )
            else:
                nested = _build_nested_apply_expr(
                    pl.field(f.name), f.dtype, field_path, per_path
                )
                if nested is not None:
                    field_exprs.append(nested.alias(f.name))
        if field_exprs:
            return base.struct.with_fields(field_exprs)
    elif isinstance(dtype, pl.List):
        item_path = f"{path}[*]"
        inner = dtype.inner
        if isinstance(inner, pl.Struct):
            field_exprs = []
            for f in inner.fields:
                field_path = f"{item_path}.{f.name}"
                if field_path in per_path:
                    mapping = per_path[field_path]
                    child = pl.field(f.name)
                    field_exprs.append(
                        V.apply_mapping_expr(child, mapping).alias(f.name)
                    )
                else:
                    nested = _build_nested_apply_expr(
                        pl.field(f.name), f.dtype, field_path, per_path
                    )
                    if nested is not None:
                        field_exprs.append(nested.alias(f.name))
            if field_exprs:
                return base.list.eval(pl.element().struct.with_fields(field_exprs))
        else:
            if item_path in per_path:
                return base.list.eval(
                    V.apply_mapping_expr(pl.element(), per_path[item_path])
                )
            if isinstance(inner, (pl.Struct, pl.List)):
                nested = _build_nested_apply_expr(pl.element(), inner, item_path, per_path)
                if nested is not None:
                    return base.list.eval(nested)
    return None


def _reuse_across_scopes(
    mapper: PIIMapper,
    target: Target,
    original: Any,
    scope: str,
) -> Optional[tuple]:
    """Find an existing replacement for the same original in another scope.

    Fake mode must map the same original to the same fake everywhere; token
    modes intentionally mint fresh tokens per occurrence.
    """
    if target.kind == "json_string":
        key_path = target.json_path
    else:
        key_path = target.path
    for entry_scope in mapper.store.scopes():
        if entry_scope == (scope or ""):
            continue
        for (es, col), entry in mapper.store._entries.items():
            if es != entry_scope:
                continue
            same_target = (
                entry.get("path") == key_path
                or col == target.column
                and entry.get("kind") == target.kind
            )
            if not same_target:
                continue
            existing = entry.get("mapping", {})
            if original in existing:
                return (existing[original], entry_scope)
    # Same-category reuse for fake mode: same original + same category in any
    # entry recorded as fake-mode mapping gets the same fake.
    if mapper.config.mode == PIIMode.FAKE or mapper.config.mode == "fake":
        for (es, col), entry in mapper.store._entries.items():
            if entry.get("category") != target.category:
                continue
            existing = entry.get("mapping", {})
            if original in existing:
                return (existing[original], es)
    return None


def _shift_value(original: Any, config: PIIConfig, state) -> Any:
    """Date shifting (roadmap 4.4): one consistent day offset per run.

    All dates move by the same offset so intervals are preserved. The offset
    comes from config.shift_days, else seeded RNG in [-30, -1] ∪ [1, 30].
    Non-date values fall back to normal fakes.
    """
    import random

    if getattr(state, "_shift_offset", None) is None:
        if config.shift_days:
            state._shift_offset = int(config.shift_days)
        else:
            rng = random.Random(config.seed)
            delta = rng.randint(1, 30)
            state._shift_offset = -delta if rng.random() < 0.5 else delta
    offset = datetime.timedelta(days=state._shift_offset)
    if isinstance(original, datetime.datetime):
        return original + offset
    if isinstance(original, datetime.date):
        return original + offset
    if isinstance(original, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                parsed = datetime.datetime.strptime(original[:10], fmt)
                shifted = parsed + offset
                return shifted.strftime(fmt)
            except ValueError:
                continue
    return original


def _perturb_value(original: Any, category: str, config: PIIConfig, state) -> Any:
    """Numeric perturbation (roadmap 4.4): scale by ±perturb_pct (default 5%).

    Deterministic per value via the run seed so the same original gets the
    same perturbed result. String digits keep their format; non-numerics
    fall back to normal fakes.
    """
    import hashlib
    import random as _random

    pct = float(config.perturb_pct) if config.perturb_pct else 5.0
    seed_src = f"{config.seed}:{original}"
    h = int(hashlib.md5(seed_src.encode()).hexdigest()[:8], 16)
    rng = _random.Random(h)
    factor = 1.0 + rng.uniform(-pct, pct) / 100.0
    if isinstance(original, bool):
        return original
    if isinstance(original, int):
        return int(round(original * factor))
    if isinstance(original, float):
        return round(original * factor, 6)
    if isinstance(original, str) and original.strip().replace(".", "", 1).replace("-", "", 1).isdigit():
        try:
            if "." in original:
                return str(round(float(original) * factor, 6))
            return str(int(round(int(original) * factor)))
        except ValueError:
            pass
    return original


def _transform_value(
    original: Any,
    category: str,
    field_name: str,
    mode: str,
    generator: PIIGenerator,
    redactor: PIIRedactor,
) -> Any:
    """Transform one python value, preserving non-string types for fakes."""
    if mode == PIIMode.REDACT or (isinstance(mode, str) and mode == "redact"):
        if not isinstance(original, str):
            original = str(original)
        return redactor._apply_redaction(
            original, redactor.config.redact_style, category, field_name
        )
    if mode == PIIMode.SHIFT or (isinstance(mode, str) and mode == "shift"):
        return _shift_value(original, redactor.config, generator.run_state)
    if mode == PIIMode.PERTURB or (isinstance(mode, str) and mode == "perturb"):
        return _perturb_value(original, category, redactor.config, generator.run_state)
    if isinstance(original, str):
        return generator.generate_unique_fake(original, category, field_name)
    if isinstance(original, bool):
        return generator.generate_unique_fake(str(original), category, field_name)
    if isinstance(original, (int, float)):
        return _unique_native_fake(
            original, category, field_name, generator,
            lambda text: _coerce_number(text, original),
        )
    if isinstance(original, datetime.datetime):
        return _unique_native_fake(
            original, "date", field_name, generator,
            lambda text: _coerce_datetime(text, original),
        )
    if isinstance(original, datetime.date):
        return _unique_native_fake(
            original, "date", field_name, generator,
            lambda text: _coerce_date(text, original),
        )
    return generator.generate_unique_fake(str(original), category, field_name)


def _unique_native_fake(original, category, field_name, generator, coerce):
    """Generate a fake whose coerced native value is unique across the run.

    String-level uniqueness is not enough: different fake strings can coerce
    to the same native value ('9' vs '009' -> 9). Claim str(coerced) so the
    mapping stays bijective after coercion.
    """
    from pii_faker.core.generators import MAX_FAKE_ATTEMPTS

    state = generator.run_state
    state.register_originals([str(original)])
    fake_text = ""
    # Once a shape's random draws have run dry, don't burn the full retries on every value.
    shape = (category, "native:" + re.sub(r"\d", "9", str(original)))
    for _ in range(3 if shape in generator._exhausted else MAX_FAKE_ATTEMPTS):
        fake_text = generator.generate_fake_value(str(original), category, field_name)
        candidate = coerce(fake_text)
        key = str(candidate)
        if not key or key == str(original):
            continue
        if key in state.originals or key in state.used_fakes:
            continue
        state.used_fakes.add(key)
        return candidate
    generator._exhausted.add(shape)
    state.warn(f"faker retries exhausted ({category}); using a non-revealing fallback")
    candidate = _native_fallback(original, generator, coerce, fake_text)
    state.used_fakes.add(str(candidate))
    state.fallbacks += 1
    return candidate


def _native_fallback(original, generator, coerce, fake_text: str):
    """
    A unique native value once random draws have run out, never derived from the
    original's value (the old `original + n` and `original#F1` fallbacks returned
    a number next to the real one, or the real date itself).
    """
    state = generator.run_state

    def free(candidate, allow_originals: bool = False) -> bool:
        key = str(candidate)
        if not key or key == str(original) or key in state.used_fakes:
            return False
        return allow_originals or key not in state.originals

    if isinstance(original, datetime.date):          # datetime is a date too
        for allow in (False, True):
            day = generator.next_free_date(lambda d, a=allow: free(coerce(d.isoformat()), a))
            if day is not None:
                return coerce(day.isoformat())
    elif isinstance(original, (int, float)) and fake_text:
        found = {}

        def claim(allow: bool):
            def check(variant: str) -> bool:
                candidate = coerce(variant)
                if free(candidate, allow):
                    found["value"] = candidate
                    return True
                return False
            return check

        shape = ("native", re.sub(r"\d", "9", fake_text))
        if generator.find_variant(shape, fake_text, False, claim(False), claim(True)) is not None:
            return found["value"]

    # Last resort: a value one digit wider than the original, from a global counter.
    width = len(str(abs(int(original)))) if isinstance(original, (int, float)) else 4
    while True:
        n = state.next_fallback()
        candidate = 10 ** (width + 1) + n
        if isinstance(original, float):
            candidate = float(candidate) + 0.25
        elif isinstance(original, datetime.datetime):
            candidate = datetime.datetime(1900, 1, 1) + datetime.timedelta(days=n)
        elif isinstance(original, datetime.date):
            candidate = datetime.date(1900, 1, 1) + datetime.timedelta(days=n)
        if free(candidate):
            return candidate


def _coerce_number(fake_text: str, original):
    digits = "".join(c for c in fake_text if c.isdigit())
    if not digits:
        digits = "0"
    try:
        return int(digits) if isinstance(original, int) else float(digits or 0)
    except ValueError:
        return fake_text


def _coerce_datetime(fake_text: str, original):
    try:
        return datetime.datetime.fromisoformat(fake_text)
    except ValueError:
        return original


def _coerce_date(fake_text: str, original):
    try:
        return datetime.date.fromisoformat(fake_text[:10])
    except ValueError:
        return original


def apply_plan(
    df: pl.DataFrame,
    plan: PlanResult,
    config: PIIConfig,
    mapper: PIIMapper,
    generator: PIIGenerator,
    redactor: PIIRedactor,
    scope: str = "",
) -> pl.DataFrame:
    """Apply a plan to one DataFrame, recording scoped mappings."""
    if not plan.has_work:
        return df

    # Free-text inline targets: collect NER spans per row and fake inline.
    free_text_targets = [t for t in plan.targets if t.kind == "free_text"]
    if free_text_targets:
        df = _apply_free_text_targets(df, free_text_targets, config, mapper, generator, redactor, scope)

    by_target: Dict[int, Dict[Any, Any]] = {}
    for idx, target in enumerate(plan.targets):
        if target.kind == "free_text":
            continue
        field_name = target.path.split(".")[-1].replace("[*]", "")
        if target.kind == "json_string":
            uniques = _collect_json_leaf_values(df, target)
        else:
            uniques = _collect_target_uniques(df, target)
        if target.kind != "json_string":
            str_originals = [u for u in uniques if isinstance(u, str) and u]
            generator.run_state.register_originals(str_originals)
        mapping: Dict[Any, Any] = {}
        for original in uniques:
            if original is None:
                continue
            if isinstance(original, str) and not original:
                continue
            reused = _reuse_across_scopes(mapper, target, original, scope)
            if reused is not None:
                mapping[original] = reused[0]
                continue
            mapping[original] = _transform_value(
                original, target.category, field_name,
                config.mode, generator, redactor,
            )
        by_target[idx] = mapping
        if target.kind == "json_string":
            mapper.add_column_mapping(
                target.column + ":" + target.json_path if False else target.path,
                target.category, mapping,
                scope=scope, kind="json_string", path=target.json_path,
                original_dtype="String", processed_dtype="String",
            )
        elif target.kind == "nested":
            mapper.add_column_mapping(
                target.path, target.category, mapping,
                scope=scope, kind="nested", path=target.path,
                original_dtype=target.dtype_name,
                processed_dtype=str(df.schema.get(target.column)),
            )
        else:
            orig_dtype = V.dtype_name(df.schema[target.column])
            proc_dtype = _processed_dtype_name(
                target.column, df.schema[target.column], config, mapping
            )
            mapper.add_column_mapping(
                target.column, target.category, mapping,
                scope=scope, kind="flat", path=target.path,
                original_dtype=orig_dtype, processed_dtype=proc_dtype,
            )

    # Apply flat targets (grouped per column natively)
    df = _apply_flat_targets(df, plan, by_target, config)
    # Apply nested targets (grouped per top-level column)
    df = _apply_nested_targets(df, plan, by_target)
    # Apply JSON-string targets
    df = _apply_json_targets(df, plan, by_target)
    return df


def _collect_target_uniques(df: pl.DataFrame, target: Target) -> List[Any]:
    """Unique non-null values at a target, preserving native types."""
    if target.kind == "flat":
        series = df[target.column]
        try:
            uniques = series.drop_nulls().unique(maintain_order=False).to_list()
        except Exception:
            uniques = series.drop_nulls().unique().to_list()
        return sorted(
            [u for u in uniques if u is not None and u != ""],
            key=lambda v: str(v),
        )
    series = _select_path_series(df, target)
    if series is None:
        return []
    try:
        uniques = series.drop_nulls().unique().to_list()
    except Exception:
        return []
    return sorted([u for u in uniques if u is not None and u != ""], key=lambda v: str(v))


def _collect_json_leaf_values(df: pl.DataFrame, target: Target) -> List[str]:
    import json as json_module

    found = set()
    for val in df[target.column].drop_nulls().to_list():
        if not isinstance(val, str):
            continue
        try:
            parsed = json_module.loads(val)
        except (ValueError, TypeError):
            continue
        for jpath, leaf in _walk_json_leaves(parsed):
            if jpath == target.json_path and isinstance(leaf, str) and leaf:
                found.add(leaf)
    return sorted(found)


def _processed_dtype_name(
    column: str,
    original_dtype: pl.DataType,
    config: PIIConfig,
    mapping: Dict[Any, Any],
) -> str:
    """Predict the post-apply dtype name for a flat column."""
    mode = config.mode
    is_redact = mode == PIIMode.REDACT or (isinstance(mode, str) and mode == "redact")
    if V.is_string_dtype(original_dtype):
        return V.dtype_name(original_dtype)
    if is_redact:
        return "String"
    if not mapping:
        return V.dtype_name(original_dtype)
    sample = next(iter(mapping.values()), None)
    if isinstance(sample, bool):
        return "Boolean"
    if isinstance(sample, int):
        return V.dtype_name(original_dtype)
    if isinstance(sample, float):
        return "Float64"
    if isinstance(sample, datetime.datetime):
        return "Datetime(us)"
    if isinstance(sample, datetime.date):
        return "Date"
    return "String"


def _apply_flat_targets(
    df: pl.DataFrame,
    plan: PlanResult,
    by_target: Dict[int, Dict[Any, Any]],
    config: PIIConfig,
) -> pl.DataFrame:
    expressions = []
    for idx, target in enumerate(plan.targets):
        if target.kind != "flat":
            continue
        mapping = by_target.get(idx, {})
        if not mapping:
            continue
        original_dtype = df.schema[target.column]
        proc_name = _processed_dtype_name(target.column, original_dtype, config, mapping)
        if V.is_string_dtype(original_dtype):
            expr: pl.Expr = V.apply_mapping_expr(pl.col(target.column), mapping)
        elif proc_name == V.dtype_name(original_dtype):
            expr = V.apply_mapping_expr(pl.col(target.column), mapping)
        else:
            cast_back = V.parse_dtype(proc_name)
            str_map = V.stringify_mapping(mapping)
            expr = pl.col(target.column).cast(pl.Utf8)
            expr = V.apply_mapping_expr(expr, str_map)
            if cast_back is not None and proc_name != "String":
                expr = expr.cast(cast_back, strict=False)
        expressions.append(expr.alias(target.column))
    if expressions:
        df = df.with_columns(expressions)
    return df


def _apply_nested_targets(
    df: pl.DataFrame,
    plan: PlanResult,
    by_target: Dict[int, Dict[Any, Any]],
) -> pl.DataFrame:
    grouped: Dict[str, Dict[str, Dict[Any, Any]]] = {}
    for idx, target in enumerate(plan.targets):
        if target.kind != "nested":
            continue
        mapping = by_target.get(idx, {})
        if mapping:
            grouped.setdefault(target.column, {})[target.path] = mapping
    expressions = []
    for column, per_path in grouped.items():
        dtype = df.schema.get(column)
        if dtype is None:
            continue
        built = _build_nested_apply_expr(pl.col(column), dtype, column, per_path)
        if built is not None:
            expressions.append(built.alias(column))
    if expressions:
        df = df.with_columns(expressions)
    return df


def _apply_json_targets(
    df: pl.DataFrame,
    plan: PlanResult,
    by_target: Dict[int, Dict[Any, Any]],
) -> pl.DataFrame:
    import json as json_module

    grouped: Dict[str, Dict[str, Dict[Any, Any]]] = {}
    for idx, target in enumerate(plan.targets):
        if target.kind != "json_string":
            continue
        mapping = by_target.get(idx, {})
        if mapping:
            grouped.setdefault(target.column, {})[target.json_path] = mapping
    for parent, path_maps in grouped.items():
        if parent not in df.columns:
            continue
        merged: Dict[str, str] = {}
        for mapping in path_maps.values():
            for original, replacement in mapping.items():
                merged[str(original)] = str(replacement)
        paths = set(path_maps.keys())

        def _transform(val: Any, _m=merged, _paths=paths) -> Any:
            if not isinstance(val, str):
                return val
            try:
                parsed = json_module.loads(val)
            except (ValueError, TypeError):
                return val
            restored = _apply_json_leaves(parsed, _m, _paths)
            try:
                return json_module.dumps(restored, ensure_ascii=False)
            except (ValueError, TypeError):
                return val

        df = df.with_columns(
            pl.col(parent)
            .map_elements(_transform, return_dtype=pl.Utf8)
            .alias(parent)
        )
    return df


def _apply_json_leaves(data: Any, mapping: Dict[str, str], paths: Any, prefix: str = "") -> Any:
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, str) and path in paths and value in mapping:
                out[key] = mapping[value]
            else:
                out[key] = _apply_json_leaves(value, mapping, paths, path)
        return out
    if isinstance(data, list):
        item_prefix = (prefix + "[*]") if prefix else "[*]"
        return [_apply_json_leaves(item, mapping, paths, item_prefix) for item in data]
    if isinstance(data, str) and prefix in paths:
        return mapping.get(data, data)
    return data


def _apply_free_text_targets(
    df: pl.DataFrame,
    targets: List[Target],
    config: PIIConfig,
    mapper: PIIMapper,
    generator: PIIGenerator,
    redactor: PIIRedactor,
    scope: str,
) -> pl.DataFrame:
    """Inline NER fakery: replace entity spans inside prose (2.6).

    Each distinct span text is faked once (shared across rows via the
    run-state), then every row's prose is rebuilt with its spans replaced.
    The per-column mapping is the union of span fakes touched by that column.
    """
    try:
        from pii_faker.core.ner import extract_spans
    except ImportError:
        return df

    df = df.clone() if hasattr(df, "clone") else df
    span_fake: Dict[str, str] = {}
    for target in targets:
        col = target.column
        if col not in df.columns:
            continue
        rows = df[col].to_list()
        by_span: Dict[str, str] = {}
        for val in rows:
            if not isinstance(val, str) or not val:
                continue
            try:
                spans = extract_spans(val)
            except ImportError:
                spans = []
            for span in spans:
                key = span.text
                if key not in span_fake and key not in by_span:
                    reused = _reuse_across_scopes(mapper, target, key, scope)
                    if reused is not None:
                        span_fake[key] = reused[0]
                        by_span[key] = reused[0]
                    else:
                        fake = _transform_value(key, span.category, target.column, config.mode, generator, redactor)
                        span_fake[key] = fake
                        by_span[key] = fake
                elif key in span_fake:
                    by_span[key] = span_fake[key]

        if not by_span:
            continue
        mapper.add_column_mapping(col, target.category, dict(by_span), scope=scope, kind="free_text", path=col)

        def _replace(val: object, _m=by_span) -> object:
            if not isinstance(val, str) or not val:
                return val
            try:
                spans_local = extract_spans(val)
            except ImportError:
                return val
            if not spans_local:
                return val
            out: list[str] = []
            cur = 0
            for span in sorted(spans_local, key=lambda s: s.start):
                fake = _m.get(span.text)
                if fake is None:
                    continue
                out.append(val[cur:span.start])
                out.append(fake)
                cur = span.end
            out.append(val[cur:])
            return "".join(out)

        df = df.with_columns(pl.col(col).map_elements(_replace, return_dtype=pl.Utf8).alias(col))
    return df




def process_dataframe(
    df: pl.DataFrame,
    config: PIIConfig,
    mapping_out: Optional[str] = None,
    selected: Optional[List[str]] = None,
    scope: str = "",
    mapper: Optional[PIIMapper] = None,
    run_state: Optional[RunState] = None,
) -> "tuple[pl.DataFrame, PIIMapper]":
    """
    Process a DataFrame through the single pipeline (plan -> apply -> mapping).

    Args:
        df: Input Polars DataFrame
        config: PII configuration
        mapping_out: Optional path to save the mapping file
        selected: Optional explicit paths to process (web UI selection)
        scope: Mapping scope (sheet/file name; "" for single-frame runs)
        mapper: Optional existing mapper to record into (multi-sheet runs)
        run_state: Optional shared run state (multi-sheet runs)

    Returns:
        Tuple of (processed DataFrame, mapper with mappings)
    """
    from pii_faker.core.config import with_user_columns

    config = with_user_columns(config)
    run_state = run_state or RunState()
    mapper = mapper or PIIMapper(config)
    generator = PIIGenerator(config, run_state)
    redactor = PIIRedactor(config, run_state)

    plan = plan_dataframe(df, config, selected)
    for skipped in plan.skipped:
        run_state.warn(
            f"column {skipped['column']!r} ({skipped.get('category', '?')}): "
            f"{skipped['reason']}"
        )
    df = apply_plan(df, plan, config, mapper, generator, redactor, scope=scope)

    if mapping_out:
        mapper.save(mapping_out)

    return df, mapper


def restore_dataframe(
    df: pl.DataFrame,
    mapper: PIIMapper,
    scope: str = "",
) -> pl.DataFrame:
    """Restore a DataFrame processed by this pipeline."""
    from pii_faker.core.restorer import PIIRestorer

    return PIIRestorer(mapper, scope).restore_dataframe(df)


def detect_targets(
    df: pl.DataFrame,
    config: PIIConfig,
) -> PlanResult:
    """Detect what process_dataframe would process (for dry runs / previews)."""
    from pii_faker.core.config import with_user_columns

    return plan_dataframe(df, with_user_columns(config), None)


def process_sheets(
    sheets: Dict[str, pl.DataFrame],
    config: PIIConfig,
    selected: Optional[List[str]] = None,
    mapping_out: Optional[str] = None,
) -> "tuple[Dict[str, pl.DataFrame], PIIMapper, RunState]":
    """
    Process multiple sheets/frames sharing one RunState and one mapper.

    The same original value gets the same fake on every sheet, and mapping
    keys are scoped per sheet so restores don't collide.
    """
    from pii_faker.core.config import with_user_columns

    config = with_user_columns(config)
    run_state = RunState()
    started_at = datetime.datetime.now()
    mapper = PIIMapper(config)
    generator = PIIGenerator(config, run_state)
    redactor = PIIRedactor(config, run_state)

    single_scope = len(sheets) == 1 and next(iter(sheets)) == "default"
    processed: Dict[str, pl.DataFrame] = {}
    all_targets: List[Any] = []
    counts: Dict[str, int] = {}
    project_mapper: Optional[PIIMapper] = None
    if getattr(config, "project_mapping", None):
        from pathlib import Path as _P

        _pp = _P(str(config.project_mapping))
        if _pp.exists():
            try:
                project_mapper = PIIMapper.load(str(_pp))
                for _k, _e in project_mapper.store._entries.items():
                    if _k not in mapper.store._entries:
                        mapper.store._entries[_k] = _e
            except Exception:
                project_mapper = None
    for sheet_name, frame in sheets.items():
        scope = "" if single_scope else sheet_name
        plan = plan_dataframe(frame, config, selected)
        for skipped in plan.skipped:
            run_state.warn(
                f"[{sheet_name}] column {skipped['column']!r}: {skipped['reason']}"
            )
        all_targets.extend(plan.targets)
        for t in plan.targets:
            key = t.path
            try:
                counts[key] = int(frame[t.column].n_unique()) if t.kind == "flat" else len(_collect_target_uniques(frame, t))
            except Exception:
                counts[key] = 0
        processed[sheet_name] = apply_plan(
            frame, plan, config, mapper, generator, redactor, scope=scope
        )

    if mapping_out:
        mapper.save(mapping_out)

    if getattr(config, "project_mapping", None) and project_mapper is not None:
        try:
            project_mapper.merge(mapper)
            project_mapper.save(str(config.project_mapping))
        except Exception:
            pass

    leak_reports = _leak_check_sheets(processed, mapper, single_scope)
    for sheet_name, report in leak_reports.items():
        if not report.clean:
            run_state.warn(f"[{sheet_name}] {report.summary()}")

    audit = None
    try:
        from pii_faker.core.audit import build_audit

        audit = build_audit(
            run_id=mapper.session_id,
            started_at=started_at,
            finished_at=datetime.datetime.now(),
            config=config,
            targets=all_targets,
            counts=counts,
            warnings=list(dict.fromkeys(run_state.warnings)),
            leak_report={"clean": all(getattr(r, "clean", True) for r in leak_reports.values())},
        )
        run_state.audit = audit  # type: ignore[attr-defined]
        if mapping_out:
            from pii_faker.core.audit import write_audit_files

            write_audit_files(audit, mapping_out)
    except Exception:
        pass

    return processed, mapper, run_state


def _leak_check_sheets(processed, mapper, single_scope: bool):
    """Leak-check processed sheets against the run mapper (never raises)."""
    from pii_faker.core.leak_check import check_sheets_with_mapper

    try:
        return check_sheets_with_mapper(processed, mapper, single_scope)
    except Exception:
        return {}


def restore_sheets(
    sheets: Dict[str, pl.DataFrame],
    mapper: PIIMapper,
) -> Dict[str, pl.DataFrame]:
    """Restore multiple sheets using scoped mappings (falls back to '')."""
    from pii_faker.core.restorer import PIIRestorer

    out: Dict[str, pl.DataFrame] = {}
    scopes = set(mapper.store.scopes())
    for sheet_name, frame in sheets.items():
        scope = sheet_name if sheet_name in scopes else ""
        out[sheet_name] = PIIRestorer(mapper, scope).restore_dataframe(frame)
    return out


ValueTransform = Callable[[Any, str, str], Any]
