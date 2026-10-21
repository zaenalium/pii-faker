# CLAUDE.md

PII Faker: detect, fake or redact PII in tabular/JSON data before it goes to an LLM, and restore it afterwards from a mapping file. Python ≥3.10, Polars, Typer CLI, FastAPI web UI. Package: `pii_faker/`.

## Commands

```bash
pip install -e ".[dev]"
python -m pytest -q                      # ~15s, 197 tests
python -m pytest pii_faker/tests/test_web.py -q
python -m pii_faker.cli.main process data.csv -o out.csv -m map.json --seed 1
python -m pii_faker.cli.main serve       # prints a sign-in link with the access token
python -m pii_faker.tests.benchmark      # throughput; ~1k unique values, so optimistic
```

Always pass `</dev/null` (or `input="n\n"` in tests) when scripting the CLI: with no PII detected it blocks on a `typer.confirm` prompt.

## Architecture

`PIIProcessor` (`core/processor.py`) is the single pipeline. The CLI, `process_dataframe()` **and the web app** all use it, so keep logic there, not in `cli/main.py` or `web/app.py`.

1. `PIIColumnDetector.detect_all(df)` finds PII by **field name** (never by value) in flat columns, Struct/List dtypes, and JSON text in `config.json_columns`.
2. `PIIGenerator` (fake) or `PIIRedactor` builds `{original: replacement}` over the **unique** values only, then Polars applies it vectorised.
3. `PIIMapper` stores one mapping per **key** and saves JSON or SQLite (chosen by file extension).
4. `PIIRestorer` reverses it.

Mapping keys are the contract between processing and restoring:

| Kind | Key | Example |
|---|---|---|
| Flat column | column name | `email` |
| Native nested | dotted path, `[*]` for lists | `patient.contacts[*].email` |
| JSON in a text cell | `column:path` | `payload:contact.phone` |

`[*]` is cosmetic: lists are traversed transparently, so `a.b.c` and `a.b[*].c` are the same field. `JsonWalker.segments(path, columns)` treats a path that is exactly a top-level column name as one segment, so flat columns with dots in their names work.

### Web app (`web/`)

- `auth.py`: one secret token, exchanged once for a random session cookie (`HttpOnly`, `SameSite=Strict`); `Authorization: Bearer` also works. `app.py` enforces it in **middleware that fails closed**: only `PUBLIC_PATHS` (`/`, `/login`) are open, so a new route is protected without remembering to. `run_server()` is the one entry point (CLI `serve` and `python -m pii_faker.web.app` both use it); it sets the token and prints the sign-in link.
- Jobs live in the in-memory `jobs` dict, each with its own `0700` temp dir under a per-server root (`_job_root`). They expire after `retention_seconds` idle (`_get_job` counts any access as activity), on `DELETE /api/jobs/{id}`, and all at once at shutdown (`lifespan`). Every code path that creates a temp dir must remove it on failure.
- Client-supplied filenames go through `_safe_filename`; never join them onto a path directly.
- The UI's ticked fields become processor entries in `_selected_entries()`. Detection for the UI is still the web's own `_detect_sheet`.

## Invariants (each one has a regression test; don't break them)

- **Every value has exactly one replacement, and replacements are distinct.** A value gets the same replacement in every column, nested path and JSON cell of its category (`PIIProcessor._shared`); different values never share one. Restore is a reverse lookup, so a shared fake silently corrupts the round trip. `PIIGenerator` guarantees per-category uniqueness (`reserved=`), preferring run-wide uniqueness (retry, then a format-preserving odometer; dates use a forward-only cursor because per-value walks were O(n²)).
- **Restore is exact match on the whole cell** (`Expr.replace`), never `str.replace_many`: with fixed-width ids one fake is often a prefix of another and substring replacement turns `1234` into `AAA4`.
- **Identifiers randomise digits *and* letters** (case and punctuation kept). Letters identify people too, and an identifier with no digits used to pass through unchanged.
- **Mapping files are `0600`**, set before any data is written, including when the file already exists. They hold real PII. SQLite saves replace the file rather than merging old rows.
- **`PIIMapper.save()` refuses ambiguous mappings** for fake and token modes (`AmbiguousMappingError`). `redact full/partial` is many-to-one by design and is exempt.
- **The CLI and web app save the mapping before writing output**, so a failed save never leaves data that can't be restored.
- **`detect()` stays top-level only.** Callers rely on that. New behaviour goes in `detect_all()`.
- **`cleanup` is narrow on purpose:** names starting `mapping` with `.json`/`.db`/`.sqlite`, in one directory, files only. The directory is user-supplied and may be a project folder (`mapping.py`).
- One `PIIProcessor` per run, shared across sheets, so a column present in several sheets gets a single combined mapping.

## Conventions

- Lossless modes are `fake` and `redact --redact-style token`. Say so in user-facing text; `full`/`partial` cannot be restored exactly.
- Categories (`name`, `identifier`, `contact`, `address`, `date`, `custom`) come from `data/default_pii_columns.yaml` and pick the generator. An explicit path keeps the category of its leaf name when the leaf matches a built-in pattern (otherwise it becomes `custom`, which generates a random word).
- A column that matches by name but holds no text (numeric phone, mistyped path) lands in `processor.skipped`; the CLI warns and the web API returns it as `skipped`. Never drop it silently.
- Tests: `test_reversibility.py` (uniqueness, identifiers, cross-column, mapping files, cleanup), `test_nested.py` (structs, lists, JSON cells), `test_cli.py` (every README example end to end, plus `serve` and `cleanup`), `test_web.py` (auth, job lifecycle, round trips), `test_core.py` (unit). New CLI/web behaviour gets a test in the matching file; after touching auth, break it on purpose and confirm the tests fail.
- Verified against Polars 1.43.2 only. `pyproject.toml` still says `polars>=1.0.0`; `Expr.struct.with_fields` may need a newer minimum. Check before publishing.

## Known gaps (found, not fixed)

- **Detection is name-only.** Free-text columns holding PII pass through untouched.
- **Web auth is one shared token**, no per-user accounts, no rate limiting (hence the 16-character minimum), plain HTTP (the server warns off-localhost). Sessions and jobs are in-memory, single process.
- **A hard-killed web server leaves its `pii_faker_web_*` temp root behind**; only a clean shutdown (or the idle timer, while running) removes it.
- **The web app keeps its own detection** (`_detect_sheet` and friends), parallel to `detect_all()`; only its processing is unified.
- **`restore_text` replaces sequentially**, longest first. A restored original could be re-matched by a later, shorter fake. Not reproduced; a single-pass approach would fix it. Cost is mapping size × text length (100k entries over 50 KB ≈ 4 s).
- **No-PII prompt blocks non-interactive runs**; there is no `--yes`.
- **`--override-defaults` drops built-in categories**, so `-e dob` is `custom` and fakes `1990-01-02` as `4026-54-23` (a shape-preserving non-date). Documented in the README; a fix would infer the category from the name.
- **SQLite mappings are larger and slower to load than JSON** (1M entries: 59 MB / 2.1 s vs 36 MB / 0.9 s); both load fully into memory.
- README is current as of this commit; every code block and number in it was run or measured. Re-check the performance table if the generator changes.
