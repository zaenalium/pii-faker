# PII Faker — agent memory

## What this project is
Python toolkit (Polars, no pandas) that detects, fakes, or redacts PII in
tabular/JSON datasets before sending them to LLMs. All transforms are
reversible via mapping files. CLI (`pii-faker`), Python API, FastAPI web UI.
Package: `pii_faker/`, tests: `pii_faker/tests/`, entry: `pii_faker/cli/main.py`.

## Source of truth
`roadmap.md` §1 (ground-check findings D1–D13) and §2 (phased plan) drive all
work. Phase 0 (all 14 items) and Phase 2 (2.1–2.5) are COMPLETE, 67 tests green.
Phase 1 (desktop, unsigned) is complete: 1.1–1.7 + 1.9 done, 1.8 signing deferred
(needs accounts). Do not start Phase 3/4 without being asked.

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
- Phase 1 (desktop, unsigned): `pii_faker/desktop.py` (random port +
  session-token middleware, background uvicorn thread, fallback to browser,
  `DesktopApi` save/open dialogs, `_check_webview2`), `POST /api/detect-path`
  + `POST /api/process-path` (no upload copy, output next to source),
  `POST /api/delete-mapping`, `platformdirs` app dirs
  (`config.app_dirs`/`mappings_dir`/`user_config_path` — legacy read intact),
  `packaging/pii-faker-*.spec` (292 MB .app → 130 MB .dmg arm64, frozen
  pipeline verified via token-auth round-trip), `.github/workflows/release.yml`
  (tag-triggered mac + Windows builds, signing TODO), `[desktop]` extra +
  `pii-faker-desktop` entry point, hardened warning + delete-mapping UX.
  Build artifacts (`dist/`, downloads) NOT cleaned — do not delete on sight.
- Native-type uniqueness subtlety: string-level claiming is NOT enough
  ('9' vs '009' → both int 9). `_unique_native_fake` claims str(coerced).
- Suite runs in ~4s; if pytest ever hangs, suspect value-detector regexes
  on large Hypothesis frames, not Polars.

## Privacy invariants, web auth and the MCP gateway
A round trip that restores exactly proves nothing about leaks: an unprocessed column
also "restores exactly". Several bugs below passed every round-trip property test. Check
the OUTPUT (`tests/test_privacy.py` does).

- **A fake never contains, or is derived from, its original.** When random draws run dry
  (`generate_unique_fake`): count through the last fake's own digits/letters
  (`_odometer`, format-preserving), sweep dates forward with a cursor
  (`next_free_date`, O(n) over a run), relax to "differs from its own original" for a dense
  shape (`find_variant` / `_dense`), and only then use `category#F{n}`. The old fallback
  was `{category}:{original}#F{n}` (and `original + n` for ints, the real date for
  `pl.Date`): 8,000 of 9,000 dense 4-digit codes and 20,260 of 30,000 DOBs came out as the
  real value inside a label. Shapes whose draws ran dry are remembered (`_exhausted`) so
  dense columns stay fast. Warnings must never quote a value either.
- **Identifiers randomise digits AND letters** (case/punctuation kept); digit-free ids
  used to pass through unchanged.
- **Nested list paths carry `[*]`** (`detect_nested_paths`). Apply/restore look fields up
  by it; without it a field was mapped, saved and silently never applied. Selection
  accepts either notation. The leak check descends into lists of structs.
- **Columns handled as JSON text (`json_columns`, selected `col:path`) are never also
  value-detected as one flat column**; that replaced every document with one fake email.
- **Mapping files are `0600`** (`_open_private` / `_make_private`, incl. encrypted and
  SQLite). `PIIMapper.save()` raises `AmbiguousMappingError` if a lossless-mode entry is
  not `bijective`.
- **Web (`web/`)**: `auth.py` (token -> `HttpOnly`/`SameSite=Strict` session cookie, or
  `Authorization: Bearer`), enforced by fail-closed middleware in `app.py` (only
  `PUBLIC_PATHS` are open). `JobStore` stamps every lookup so idle jobs expire
  (`purge_expired_jobs`, lifespan reaper, purge at shutdown, `DELETE /api/jobs/{id}`).
  `/api/detect-path` and `/api/process-path` read/write arbitrary paths, so they need
  `app.state.allow_local_paths` (only the desktop launcher sets it). The desktop's
  `create_app` also installs `Authenticator(session_token)`, disables idle expiry
  (`retention_seconds = 0`: its Delete-mapping button needs the job), and its window opens
  `/?token=...`. `create_app` adds middleware to the shared app for good, so
  `test_web.py` exercises it in a subprocess.
- **MCP gateway (`mcp_server.py`, optional `mcp` extra)**: everything a tool returns goes
  to the LLM provider. `Gateway` plans with `plan_dataframe`, applies with `apply_plan`
  (one `RunState`/`PIIMapper`, scope = dataset name, so a person is the same across
  datasets) and tools read only the faked, pruned copy. Exposure is an ALLOWLIST
  (`Policy`): faked or `--keep` only. Unprocessable targets (`plan.skipped`, which also
  appear in `plan.targets`) and free text are blocked; structures the detector can't see
  (a list of lists) are dropped. Every tool is wrapped in `_safe`: only `GatewayError`
  text reaches the model and only the exception TYPE is logged (Polars quotes cells; SDK 2.2
  hides the text from the client but logs the traceback). The model never supplies a path;
  `restore_to_file` returns only `{saved_as, replacements}`. HTTP wraps the whole ASGI app in
  `BearerAuthMiddleware` and sets Host/Origin checks, so a tunnel needs `--allowed-host`.
  Everything the command prints goes to stderr (stdout is the stdio protocol). Refuses
  partial/shift/perturb. SDK is `mcp` 2.x: `MCPServer` in `mcp.server.mcpserver`, HTTP client
  takes an `httpx2` client.
- After touching auth, expiry, path gating or the gateway, BREAK the property on purpose
  (e.g. `Authenticator.is_authenticated = lambda *a: True`, `Policy.allows = lambda *a: True`)
  and confirm a test fails. To mutate a FastAPI dependency, patch the function's `__code__`:
  rebinding the module name does nothing, the route captured the original.

## Conventions
- Polars only, never pandas. Native expressions in hot paths; `map_elements`
  only for JSON-string leaves (unique values, cold path).
- Tests: `python -m pytest -q` (269 tests, ~20s, must stay green). Needs the dev extra
  (Hypothesis, pytest-asyncio) and, for `test_mcp.py`, the `mcp` extra.
- Commit messages end with `Co-authored-by: CommandCodeBot <noreply@commandcode.ai>`
  and are passed via HEREDOC — but only commit when explicitly asked.
