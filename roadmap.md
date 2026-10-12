# PII Faker Roadmap

Last reviewed: 2026-10-11 (against commit `142aeb0`)
Status update: 2026-10-11 — Phase 0 implementation started (see §2 status column).
Existing suite (41 tests) still green after the changes below.
Update 2026-10-11 (later): all Phase 0 code items complete — 51 tests green
(41 existing + 10 Hypothesis round-trip tests). Exit-criteria smoke test still
to run (README examples end-to-end on all 3 OSes via CI).

This roadmap comes from checking the README's claims and the proposed improvements against how the code actually behaves. Every finding below was reproduced by running the CLI, the Python API or the web app, not only by reading the code. The existing test suite (41 tests) passes, but it doesn't cover any of the failures listed here.

---

## 1. Ground-check results

### 1.1 Correctness and safety defects (not in the original idea list, must come first)

| # | Finding | Evidence | Impact |
|---|---------|----------|--------|
| D1 | **Nested JSON paths are never processed by the CLI.** `--extra-pii-columns "patient.patient_name"` on NDJSON reports "No PII columns detected". | `cli/main.py` only calls `detector.detect(df)`, which matches top-level names | README Scenarios B and C don't work |
| D2 | **`--json-columns` and `--strict-paths` are parsed but unused.** | Grep: no consumer outside `config.py` | README Scenario A doesn't work in the CLI |
| D3 | **Web UI detects nested struct fields, then leaks them.** `patient.patient_name` shows up as detected and selectable, but the output still contains the real names. | `web/app.py` `_process_sheet` filters top-level detections by the selected paths | **Silent PII leak**, worse than no detection |
| D4 | **JSON-string columns are faked by the web UI but can't be restored.** | Mapping is keyed `metadata:email`, and the restorer only matches real column names | Fake mode is no longer reversible for these columns |
| D5 | **Fake values collide.** 20k names produced 2,182 duplicate fakes, so 2,475 rows restored to the wrong person. 5,000 4-digit `member_code` values produced 1,036 wrong rows. | `generators.py` doesn't enforce uniqueness, and the reverse dict overwrites duplicates | Breaks the core promise that fake mode is fully reversible |
| D6 | **Non-string columns are skipped silently.** An integer `phone` and a `Date`-typed `dob` pass through unchanged. | `generators.py` / `redactor.py`: `if isinstance(original, str)` | **Silent PII leak**, common with Parquet, Excel and inferred CSV types |
| D7 | **`restore_text` chains replacements and ignores word boundaries.** `{"Ann":"Bob","Bob":"Carl"}` turns "Bob met Carl." into "Ann met Ann.", and fake "Jo" turns "John" into "Alicehn". | `restorer.py` loops `str.replace` | Restored LLM output can be wrong without any warning |
| D8 | **Token redaction collides across columns.** Each column restarts at `REDACTED_0001`, so restoring text maps every token to one column's originals. Token order also isn't sorted, so it isn't deterministic. | `redactor.py` resets `token_counter` per column | "Token style is lossless" is false for multi-column data |
| D9 | **Multi-sheet runs lose mappings.** With `--sheets S1,S2`, the mapping keeps only S2's `name` values, so S1 can't be restored. The same original also gets different fakes on different sheets. | `MappingStore.add_mapping` overwrites by column name | Data loss |
| D10 | **`--all-sheets` crashes** with `'DataFrame' object has no attribute 'keys'`. | `file_io.py`: `read_excel(sheet_name=None)` returns a DataFrame | Feature broken |
| D11 | **`pii-faker columns add` has no effect on `process`.** The value is written to `~/.pii_faker/config.yaml` but never read. | Grep: no reader | Feature broken |
| D12 | **Web temp files are never deleted.** Uploaded originals and plaintext mappings stay in the OS temp directory indefinitely. | `web/app.py`: `mkdtemp` with no cleanup | PII left on disk |
| D13 | **Upload filename is joined into a path unsanitised** (`temp_dir / file.filename`). | `web/app.py` | Path traversal or overwrite; low risk on localhost, but must be fixed before shipping a desktop app |

### 1.2 README claims that don't match the code

- **"Lazy streaming supported" (CSV, NDJSON):** `read_lazy()` exists but nothing calls it. Every read is eager.
- **"Mapping contains session ID":** the session ID is generated but not saved.
- **"Default location `~/.pii_faker/mappings/`":** nothing writes there by default. `cleanup` only looks there and only matches `mapping_*` filenames.
- **Nested JSON and JSON-string support:** see D1 to D4.
- **"SQLite mapping for large runs":** SQLite only changes the file format. The full mapping is still loaded into memory, both to save and to restore.

### 1.3 Proposed improvements: verdicts

| Idea | Verdict | Notes from ground check |
|------|---------|-------------------------|
| Desktop app (Option A: native window + bundled server) | ✅ **Valid, chosen** | Feasible. The web UI is FastAPI + Jinja + one `index.html` with **no CDN dependencies**, so it works offline as is. The prerequisites are D3, D4, D12 and D13, plus merging the three separate processing pipelines (CLI, web, `core.process_dataframe`) into one. |
| Value-based PII detection | ✅ **Valid, high priority** | Confirmed gap. `col_7` containing emails, `notes` containing phone numbers, and `mobile` and `nik` columns are all missed. Defaults have only 15 patterns. Fuzzy mode gives false positives on `username`, `company_name` and `filename`. |
| Leak check (re-scan output) | ✅ **Valid** | Depends on value-based detection. It would have caught D3 and D6. |
| Encrypted mappings + auto-expiry | ✅ **Valid** | Mappings are plaintext JSON or SQLite, and D12 makes this worse. |
| Audit report per run | ✅ **Valid, cheap** | |
| LLM round-trip screen | ✅ **Valid, but blocked** | D7 and D8 must be fixed first. The safe LLM default should be typed, globally unique tokens (`<PERSON_1>`), not the current `REDACTED_0001`. |
| Date shifting / numeric perturbation | ✅ **Valid** | Dates are currently random DOBs, and `Date` dtype is skipped entirely (D6). |
| Project-level mappings across files | ✅ **Valid** | Fixing D9 does most of the groundwork (mapping keys must include sheet/file and stay consistent). |
| Compliance presets | ⚠️ **Narrow it** | The code mentions `tanggal_lahir`, which suggests Indonesian data. An **Indonesia preset** (NIK, NPWP, BPJS, `id_ID` Faker locale, UU PDP fields) is worth more than generic HIPAA/GDPR presets. Faker is currently hard-coded to `en_US`. |
| Local proxy / MCP server | ⚠️ **Defer** | Prompts are free text, so this needs reliable free-text detection (NER) and a correct text restore first. Treat it as exploratory. |
| Keyed-hash (HMAC) pseudonymization | ⬇️ **Drop for now** | Little user value compared with the other items. |
| Homebrew / winget / Scoop | ⬇️ **Drop** | Redundant once signed desktop installers exist. Keep `pipx` in the README for CLI users. |
| Cross-platform CI + property-based round-trip tests | ✅ **Valid, do early** | There's no CI today. A property test (`restore(process(x)) == x`) would have caught D5, D8 and D9. |

---

## 2. Roadmap

Sizes: **S** ≈ under a day, **M** ≈ 2–4 days, **L** ≈ a week or more.

### Phase 0: Make the core trustworthy (v0.2)
*Goal: every README claim is true, and fake/token modes are actually lossless. Nothing else should ship before this.*

| ID | Task | Size | Fixes | Status 2026-10-11 |
|----|------|------|-------|-------------------|
| 0.1 | Add GitHub Actions CI (Linux, macOS, Windows × Python 3.10–3.13) | S | — | 🟢 done (`.github/workflows/ci.yml`; hypothesis added to dev extras) |
| 0.2 | Add property-based round-trip tests (Hypothesis) for fake + token, multi-column, multi-sheet, nested | M | Guards D5, D8, D9 | 🟢 done (`tests/test_roundtrip.py`: 10 tests — flat, multi-column, multi-sheet incl. same-fake-everywhere, token uniqueness, typed-token, nested, JSON-string, date, int, text no-chaining) |
| 0.3 | **Single processing pipeline** in `core` (`plan → apply → mapping`) used by CLI, web and Python API; delete the duplicated logic in `web/app.py` | L | D1–D4 root cause | 🟢 done (`core/pipeline.py`: `plan_dataframe`/`apply_plan`/`process_sheets`/`restore_sheets`; CLI + web + `core.process_dataframe` all route through it; ~400 lines of web helper duplication deleted) |
| 0.4 | Nested struct / list-of-struct processing via native Polars expressions (`struct.with_fields`, `list.eval`), honouring `--strict-paths` | L | D1, D3 | 🟢 done (process: `_build_nested_apply_expr` + `_select_path_series` collect; restore: `_build_nested_restore_expr`; explicit dotted extras resolved via `_resolve_extra_paths`; verified CLI NDJSON end-to-end with lossless restore) |
| 0.5 | JSON-string columns: process in the CLI and make the mapping restorable | M | D2, D4 | 🟢 done (`--json-columns` plans `json_string` targets, `kind="json_string"` entries, restore via `restore_json_columns`; verified CLI end-to-end with lossless restore) |
| 0.6 | Enforce fake uniqueness per mapping (retry, then suffix fallback) and reject fakes that equal any original | M | D5 | 🟢 done in code (`core/state.py` `RunState` + `generators.generate_unique_fake`: 50 retries, `{category}:{original}#F{n}` fallback, `used_fakes`/`originals` shared per run; needs property-test proof) |
| 0.7 | Handle non-string dtypes: cast-and-restore for ints; `Date`/`Datetime` support; warn loudly if a selected column is skipped | M | D6 | 🟢 done (fake: native-type fakes for int/float/date/datetime, cast-and-restore for changed dtypes, skipped dtypes recorded + surfaced as CLI warnings / web `warnings`; restore casts back via `original_dtype`) |
| 0.8 | Rewrite `restore_text` as single-pass longest-match scan with word-boundary option | M | D7 | 🟢 done in code (`restorer.restore_text`: single-pass alternation regex, longest-first, cached; `word_boundaries` flag. Note: roadmap said `str.replace_many`, but that is first-match not longest-match — verified empirically — so regex was used instead) |
| 0.9 | Globally unique tokens with deterministic order; add a typed token style `<PERSON_1>` / `<EMAIL_3>` | S | D8 | 🟢 done in code (counter moved to run-shared `RunState`; sorted unique-values order; `typed-token` style + `make_typed_token` with EMAIL/PHONE field hints; needs property-test proof) |
| 0.10 | Mapping keys include sheet/file scope; reuse one generator per run so the same original gets the same fake everywhere | M | D9 | 🟢 done (scoped v2 keys + `_reuse_across_scopes`: same original + same category reuses the fake across sheets; verified: John → same fake on S1/S2, both restore) |
| 0.11 | Fix `--all-sheets` | S | D10 | 🟢 done (`file_io.py`: sheet names via `fastexcel.read_excel(path).sheet_names`; `sheet_name=None` returns one DataFrame in current Polars) |
| 0.12 | Load `~/.pii_faker/config.yaml` custom columns in `process` (and the web UI) | S | D11 | 🟢 done (`config.with_user_columns` merged in `process_dataframe`/`process_sheets`/`detect_targets`, so CLI + web + API all pick it up) |
| 0.13 | Bump the mapping format to v2 (session ID, scope keys, tool version) with a loader for v1 | S | README claim | 🟢 done in code (`mapper.py`: v2 `pairs` with tagged native-type values, `session_id` persisted, SQLite schema migrated with `DROP TABLE` recreate, v1 JSON + v1 SQLite loaders) |
| 0.14 | Fix the README to match reality (streaming, default mapping location, SQLite caveat) | S | §1.2 | 🟢 done (streaming caveat, no-default-location, SQLite memory note, typed-token + v2 + web hardening notes, multi-sheet sibling-file note) |

**Exit criteria:** property tests pass on all 3 OSes (CI added; Mac run is green
locally — 51 passed). Every README example runs as written in an end-to-end smoke test, and none leaks.

### Phase 1: Desktop app for macOS and Windows (v0.3)
*Goal: double-click installers that run fully offline. Uses Option A: a native window (pywebview) around the bundled FastAPI app.*
*Status 2026-10-11: 1.1–1.6 done and verified on this arm64 Mac; 1.7 build
matrix present; 1.8 signing deferred (needs accounts); 1.9 WebView2 check
present.*

| ID | Task | Size | Notes | Status |
|----|------|------|-------|--------|
| 1.1 | Web hardening: sanitise filenames, delete temp dirs after download or on exit, cap upload size | S | D12, D13 | 🟢 done (hardened in Phase 0; reused here) |
| 1.2 | Desktop launcher: start the server on `127.0.0.1` with a random port and a per-launch session token; open it in a pywebview window; shut down cleanly | M | Token blocks other local processes from calling the API | 🟢 done (`pii_faker/desktop.py`: random port + `token_urlsafe(32)` + `X-PII-Session` middleware; clean shutdown; fallback to browser when pywebview unavailable) |
| 1.3 | Native file dialogs and **path-based processing** (no browser upload copy) for large files; drag-and-drop | M | Avoids loading files into memory twice | 🟢 done (`DesktopApi` JS bridge + `POST /api/detect-path` + `POST /api/process-path` — reads in place, outputs next to source; drag-and-drop already present) |
| 1.4 | App-data directories via `platformdirs` for config, mappings and logs | S | Replaces `~/.pii_faker` | 🟢 done (`config.app_dirs`/`mappings_dir`/`user_config_path`; CLI `columns`/`cleanup` + config loader migrated; legacy `~/.pii_faker` still read) |
| 1.5 | Prominent "mapping file contains real PII" UX: save-location prompt, warning badge, one-click "delete mapping" | S | | 🟢 done (hardened warning copy + save-location `<code>` + Delete button → `POST /api/delete-mapping`, PII-free delete) |
| 1.6 | Packaging with PyInstaller (or Briefcase): `.app` / `.dmg` (universal2, or separate arm64 + x86_64) and `.exe` installer | M | Expect a 150–250 MB bundle (Polars, PyArrow, fastexcel) | 🟢 done (`packaging/*.spec` with heavy-dep excludes; arm64 `.app` 292 MB → `.dmg` 130 MB; frozen pipeline verified via token-auth API round-trip) |
| 1.7 | GitHub Actions release matrix (no cross-compiling: build on `macos-latest` and `windows-latest`) | M | | 🟢 done (`.github/workflows/release.yml`: tag-triggered mac (`.dmg`) + Windows (`.exe`), upload-artifact) |
| 1.8 | Code signing: Apple Developer ID + notarization; Windows signing via Azure Trusted Signing or SignPath (OSS) | M | Needs accounts and secrets; Apple is $99/yr. Unsigned builds trigger Gatekeeper/SmartScreen warnings, which undermines trust in a privacy tool. | ⬜ deferred (unsigned builds shipped — signing needs Developer ID / Trusted Signing secrets; TODO noted in release workflow) |
| 1.9 | Check WebView2 at install time on Windows (built into Windows 11 and updated Windows 10) | S | | 🟢 done (`_check_webview2` preflight + hint, wired into `main`) |

**Exit criteria:** a signed, notarized build installs and processes a CSV, an XLSX and an NDJSON file offline on a clean macOS and a clean Windows machine, with no security warnings.
Unsigned arm64 build verified on this machine (UI shell + token-gated
`/api/detect-path` → `/api/process-path` round-trip); signing still pending.

> Ordering note: if the desktop app targets non-technical users, consider shipping Phase 2.1–2.3 before announcing 1.0. Those users won't know to check whether detection missed a column.

### Phase 2: Detection that doesn't rely on column names (v0.4)
*Goal: catch PII wherever it lives, and prove the output is clean.*
*Status 2026-10-11: code items 2.1–2.5 complete, 67 tests green (51 + 16 new
detection tests). 2.6 (optional NER extra) deferred as planned.*

| ID | Task | Size | Notes | Status |
|----|------|------|-------|--------|
| 2.1 | Value-based detectors on a sample of rows: email, phone, credit card (Luhn), IBAN, IP, URL-with-credentials, plus Indonesian NIK (16 digits with region/date checks), NPWP and BPJS | M | Vectorised `str.contains` over a sample | 🟢 done (`core/value_detectors.py`: 9 detectors, Luhn + NIK plausibility, wired into `plan_dataframe` — value-only columns become targets) |
| 2.2 | Confidence score per column (name match + value match ratio), shown in the CLI dry run and the GUI preview | S | | 🟢 done (`detect_with_confidence`: 0.5·name + 0.5·value; dry-run Confidence/Signal columns; GUI shows `category · conf · signal`) |
| 2.3 | **Leak check:** re-scan the output with 2.1 and fail or warn if residual PII is found; on by default in the GUI | M | | 🟢 done (`core/leak_check.py`: original-value + value-shape scans, PII-free reports; runs in `process_sheets`, surfaces as CLI warnings + web `warnings`; verified: deselected column flagged, clean run silent) |
| 2.4 | Expand default column patterns (first/last/full name, mobile, NIK, NPWP, tanggal_lahir, alamat, …) and tighten fuzzy matching (token-based instead of character-set overlap) | S | Removes the `username`/`filename` false positives | 🟢 done (15 → 34 patterns incl. Indonesian set; token-based fuzzy + `NON_PII_QUALIFIERS` guard — `username`/`filename`/`company_name` excluded, compounds still match) |
| 2.5 | Faker locale option (`--locale id_ID`); auto-suggest it from detected identifiers | S | | 🟢 done (`PIIConfig.locale`, `--locale` + web `locale` field, `suggest_locale` in dry run + web `suggested_locale`) |
| 2.6 | Optional free-text NER for names and addresses inside text columns (Presidio, spaCy or GLiNER) as an extra (`pip install pii-faker[nlp]`) | L | Keep it optional so the desktop bundle stays smaller | ⬜ deferred (as planned) |

**Exit criteria:** the benchmark fixture with renamed columns and free-text notes is fully caught, and the leak check reports zero residual PII.
Verified 2026-10-11: `col_7` (emails) and `notes` (phones) auto-detected via
value shapes; `username`/`company_name`/`filename` excluded; leak check flags
a deselected PII column and is silent on a clean run.

### Phase 3: Safety and accountability (v0.5)
*Status 2026-10-12: 3.1–3.4 complete — 6 new tests (73 green), encrypted
CLI round-trip verified end-to-end, web preset list + audit download
verified, PII-free audits proven by grep.*

| ID | Task | Size | Notes | Status 2026-10-12 |
|----|------|------|-------|-------------------|
| 3.1 | Encrypted mapping files (AES-GCM via `cryptography`); key stored in macOS Keychain / Windows Credential Manager (`keyring`), with an optional passphrase | M | | 🟢 done (`core/crypto.py` envelope + passphrase/env/keychain hierarchy; CLI `--encrypt/--passphrase`, `key set/show/delete`; verified: encrypted file opaque on disk, `--passphrase` round-trip restores exactly) |
| 3.2 | Auto-expiry: the app deletes mappings older than N days (setting); make `cleanup` find mappings by format, not filename | S | | 🟢 done (format probe + `retention_days`/`auto_expire_mappings`; tested: ours expire, alien files survive) |
| 3.3 | Audit report per run (JSON + human-readable): columns, counts, mode, seed, leak-check result, tool version. **Must contain no PII.** | S | | 🟢 done (`core/audit.py`; CLI writes `*_audit.json/.txt`, `--audit-out` supported; web `GET /api/download/{id}/audit` + audit button verified; `grep Budi audit.json` = 0) |
| 3.4 | Indonesia preset (UU PDP-oriented column set + `id_ID` locale + NIK/NPWP/BPJS detectors); keep the preset format generic so others can be added | S | | 🟢 done (generic `data/presets/*.yaml` + `presets list/show`, `--preset id`, `GET /api/presets` + dropdown verified; `id_ID` fakes confirmed: `Kairav Haryanto`, `phalimah@example.com`) |

**Exit criteria:** `process --encrypt` + `restore` round-trips exactly (verified
`/tmp` fixture); cleanup deletes only our format (tested); every run writes a
PII-free audit (verified by grep + unit test); `--preset id` wires locale +
columns (verified end-to-end).

### Phase 4: LLM round-trip workflow (v0.6)

| ID | Task | Size | Notes |
|----|------|------|-------|
| 4.1 | GUI round-trip screen: processed file → "copy for LLM" → paste the LLM response → restored text, with restored spans highlighted | M | Depends on 0.8, 0.9 |
| 4.2 | Make typed tokens (`<PERSON_1>`) the default for LLM use and explain why: realistic fakes get rewritten by models ("Mr. Smith"), tokens rarely do | S | |
| 4.3 | Tolerant restore for realistic fakes: case-insensitive, possessives, first-name-only and last-name-only matches; flag ambiguous matches instead of guessing | M | |
| 4.4 | Date shifting (one consistent offset per entity, so intervals are preserved) and numeric perturbation as additional modes | M | |
| 4.5 | Project mode: one mapping shared across multiple files so a person gets the same fake everywhere | M | Builds on 0.10 |

### Phase 5: Exploratory (no commitment)

- **Local LLM proxy or MCP server**: an OpenAI/Anthropic-compatible local endpoint that fakes outgoing prompts and restores responses. Only worth starting once 2.6 and 4.3 are reliable.
- **Real streaming** for very large CSV/NDJSON (`scan_*` + `sink_*`, with mapping built from a unique-values pass).
- **Disk-backed mapping lookups** so SQLite mappings don't load fully into memory.

---

## 3. Dependency summary

```
Phase 0 (core correctness) ──┬──> Phase 1 (desktop app)
                             ├──> Phase 2 (detection) ──> 2.3 leak check ──> Phase 5 proxy
                             └──> 0.8/0.9 ──> Phase 4 (round-trip)
2.1–2.3 ──> recommended before promoting the desktop app to non-technical users
```
