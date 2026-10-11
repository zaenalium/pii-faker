# PII Faker — agent memory

## What this project is
Python toolkit (Polars, no pandas) that detects, fakes, or redacts PII in
tabular/JSON datasets before sending them to LLMs. All transforms are
reversible via mapping files. CLI (`pii-faker`), Python API, FastAPI web UI.
Package: `pii_faker/`, tests: `pii_faker/tests/`, entry: `pii_faker/cli/main.py`.

## Source of truth
`roadmap.md` §1 (ground-check findings D1–D13) and §2 (phased plan) drive all
work. Phase 0 (all 14 items) and Phase 2 code items (2.1–2.5) are COMPLETE as
of 2026-10-11, 67 tests green. 2.6 (optional NER) deferred as planned. Do not
start Phase 1/3/4 without being asked.

## Verified Polars behaviors (don't re-derive; tested empirically)
- `pl.field(name)` — not `pl.col(name)` — for relative refs inside
  `struct.with_fields`; `pl.element()` inside `list.eval` (incl. nested
  struct-in-list). Deep chains like `pl.col('p').struct.field('c')...` ADD
  fields instead of replacing.
- `Expr.str.replace_many` is FIRST-match, not longest-match (order of patterns
  doesn't matter). Hence `restore_text` uses a single-pass alternation regex,
  longest-first, not `replace_many`.
- `Expr.replace(mapping)` on Int cols with str values FAILS (no implicit
  cast); str cols with int values silently coerce to str. So int columns use
  cast-process-cast-back with dtypes recorded in the mapping.
- `replace` with `default=` is deprecated in Polars ≥1.0 (use
  `replace_strict`); existing tests still pass but emit warnings.
- `pl.read_excel(sheet_name=None)` returns ONE DataFrame in current Polars —
  discover sheet names via `fastexcel.read_excel(path).sheet_names`.
- `DataFrame.write_excel(path, worksheet=...)` OVERWRITES the file per call;
  multi-sheet writes need one `xlsxwriter.Workbook` session.
- `str.json_decode` requires an explicit `dtype` — unsuitable for arbitrary
  JSON strings; JSON-string columns are handled via Python `json` in
  `map_elements` (cold path, unique-values only).
- `Series.replace({})` is a no-op (safe); `replace` preserves nulls.

## Current architecture (Phase 0 complete)
- `core/pipeline.py`: THE single pipeline — `plan_dataframe` (flat + nested +
  json_string targets, explicit `selected` paths, strict-paths, skipped-dtype
  warnings), `apply_plan` (native flat/nested/JSON apply, scoped v2 mappings,
  `_reuse_across_scopes` for same-fake-everywhere), `process_dataframe`,
  `process_sheets`, `restore_sheets`, `detect_targets`. CLI, web, and Python
  API all route through it.
- `core/state.py`: `RunState` — per-run `used_fakes`, `originals`, global
  `token_counter`, fallback counter, warnings. Shared across all targets and
  scopes in a run.
- `core/values.py`: shared dtype helpers (`dtype_name`/`parse_dtype`,
  `is_supported_leaf`, `apply_mapping_expr` via `replace_strict`, native-type
  coercion).
- `core/generators.py`: `generate_unique_fake` (50 retries →
  `{category}:{original}#F{n}` fallback, max 2^32). Non-string fakes keep
  native types (int/float/date/datetime).
- `core/redactor.py`: counter lives in `RunState` (globally unique tokens);
  `typed-token` style (`core/config.py`) via `make_typed_token`
  (`<PERSON_1>`, `<EMAIL_3>` w/ field-name hints). Sorted unique values =
  deterministic order.
- `core/mapper.py`: v2 format — `pairs` with tagged native-type values,
  `(scope, column)` keys, `session_id`, `tool_version`, entry `kind`
  (`flat`/`nested`/`json_string`), dtype metadata. Loads v1 JSON + v1 SQLite.
- `core/restorer.py`: scope-aware `PIIRestorer(mapper, scope)`; dtype-aware
  leaf restore; native nested struct/list restore; `restore_json_columns`;
  single-pass longest-match `restore_text` with cached regex +
  `word_boundaries` flag. Uses `replace_strict` (no deprecation warnings).
- `core/config.py`: `load_user_extra_columns` / `with_user_columns` —
  `~/.pii_faker/config.yaml` merged in every entry point (D11 fixed for
  CLI + web + API).
- Web hardening (Phase 1 task 1.1, done early): sanitised filenames,
  500 MB upload cap, temp-dir cleanup via BackgroundTask after download,
  error-path cleanup.
- Tests: `tests/test_roundtrip.py` — 10 Hypothesis property tests
  (flat/multi-column/multi-sheet/nested/JSON-string/date/int/token/
  typed-token/text); `tests/test_detection.py` — 16 tests (value shapes,
  Luhn/NIK vectors, confidence, fuzzy exclusions, leak check, locale).
  Full suite: 67 passed.
- Phase 2 additions: `core/value_detectors.py` (9 regex detectors over a
  ≤500-row sample + Luhn + NIK plausibility + `suggest_locale`),
  `core/leak_check.py` (original-value + value-shape re-scan, PII-free
  reports, runs inside `process_sheets`), `Target.confidence/name_match/
  value_detector`, `PIIConfig.locale` (`--locale`, web `locale` field),
  34 default patterns (Indonesian set), token-based fuzzy with
  `NON_PII_QUALIFIERS`.
- Native-type uniqueness subtlety: string-level claiming is NOT enough
  ('9' vs '009' → both int 9). `_unique_native_fake` claims str(coerced).
- Suite runs in ~4s; if pytest ever hangs, suspect value-detector regexes
  on large Hypothesis frames, not Polars.

## Conventions
- Polars only, never pandas. Native expressions in hot paths; `map_elements`
  only for JSON-string leaves (unique values, cold path).
- Tests: `python -m pytest -q` (41 tests, must stay green). Hypothesis is
  available (6.168.2) for the planned property tests.
- Commit messages end with `Co-authored-by: CommandCodeBot <noreply@commandcode.ai>`
  and are passed via HEREDOC — but only commit when explicitly asked.
