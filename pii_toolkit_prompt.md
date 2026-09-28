# Project Prompt: PII Fake/Redact Toolkit (CLI + Web) for LLM-Safe Data Sharing

## Context & Goal

I need a **Python package** that detects, and then either **fakes (synthesizes)** or **redacts** PII (Personally Identifiable Information) in tabular and JSON datasets **before that data is sent as an attachment to an LLM**.

The critical requirement: **every fake/redacted value must be reversibly mapped back to its original value**, so that once the LLM returns results referencing the faked/redacted data, we can restore the original values losslessly.

This needs to work as:
1. A **Python package** importable in other code.
2. A **CLI tool** runnable on Linux, macOS (bash/zsh), and Windows (cmd.exe and PowerShell).
3. A **simple local web app** with file upload, PII column selection, redact/fake toggle, and download of the result.

**Performance is a primary design goal.** This will run over large exports, and it sits in the hot path before every LLM submission.

---

## Hard Constraint: Use Polars, Not pandas

**Do not use pandas anywhere in this project.** Use **Polars** as the dataframe engine.

Rationale to respect while building:
- Polars is Rust-backed, Arrow-native, multi-threaded by default, and has a lazy API for query optimization.
- Polars has **first-class nested types** (`Struct`, `List`), which matters enormously for the JSON requirements below. pandas would force nested data into opaque `object` columns.
- Lower memory footprint on wide exports.

Specific Polars guidance I want followed:

- **Avoid `map_elements()` in hot paths.** It drops to a per-row Python callback and destroys the performance advantage. Use native expressions.
- For applying the original→fake mapping to a column, use **`Expr.replace_strict()`** (or `replace()`) with the mapping dict, or a **join against a small mapping DataFrame** — not a row-wise UDF.
- For the free-text restore step (finding faked values inside an LLM's prose response), use **`Expr.str.replace_many()`**, which is Aho–Corasick backed and handles thousands of simultaneous patterns in a single pass. Do not loop regex substitutions.
- Use the **lazy API (`scan_csv`, `scan_ndjson`, `LazyFrame`)** where the format supports it, collecting only once at the end.
- Excel I/O: `pl.read_excel()` with the **`calamine` engine** (via `fastexcel`) is the fast reader — prefer it over `openpyxl`/`xlsx2csv`. For writing, `DataFrame.write_excel()` uses `xlsxwriter`. Note in the README that Excel is inherently the slowest format here and that CSV/NDJSON will be dramatically faster if the user controls the export.

### The Real Bottleneck (please architect around this)

The dataframe engine is **not** where the time goes in this workload — the fake-data generation is, because `Faker` calls are per-value Python. So:

- **Build the mapping over unique values only.** Run `col.unique()` (or `.drop_nulls().unique()`), generate fakes for that set once, then apply the mapping back to the full column vectorized. On a 5M-row file with 40k distinct patient names, this is ~125x fewer Faker calls.
- This also **automatically satisfies the consistency requirement** (same original value → same fake value everywhere), since the mapping is keyed on the unique value.
- Cache/reuse the mapping across columns of the same PII category where semantically appropriate, but keep separate namespaces per column by default so two different columns don't collide.

### Alternative Engines (evaluate, then justify your choice)

I'm specifying Polars, but if you see a strong reason to differ, tell me before proceeding:
- **DuckDB** — excellent JSON path querying (`json_extract`, `->>`), SQL-native, great out-of-core handling for files bigger than RAM. Reasonable as a *complement* to Polars for complex nested JSON path selection, if that proves cleaner than Polars struct expressions.
- **PyArrow** — lower level; fine for I/O but you'd end up rebuilding what Polars gives you.
- **pandas** — explicitly rejected.

Default plan: **Polars for everything**, with DuckDB considered only if nested JSON path handling gets unwieldy. Flag it to me rather than silently adding a second engine.

---

## Default PII Column List

Ship a default, built-in list of PII column name patterns as a **package resource** (e.g. `pii_toolkit/data/default_pii_columns.yaml`), not hardcoded in Python:

```
name
patient_name
member_name
patientcardnumber
card_number
card_no
phone
store_phone
email
address
member_code
additional_member_code
```

Requirements:
- Matching against real headers must be **normalized** (case-insensitive; ignore spaces/underscores/hyphens) so `Patient Name`, `patient-name`, and `PATIENT_NAME` all match `patient_name`.
- Support **fuzzy/partial matching** as opt-in (so `patients_full_name_2024` is catchable), but default to normalized matching to limit false positives. Strategy configurable: `exact` | `normalized` | `fuzzy`, default `normalized`.
- Each entry maps to a **PII category** (`name`, `identifier`, `contact`, `address`, `code`) so the faker generates category-appropriate values — fake names for name fields, valid-looking emails for email fields, format-preserving card numbers for card fields.

---

## JSON & Nested JSON Support (Required)

This is a first-class requirement, not an afterthought. Three distinct scenarios must all work:

### Scenario A — JSON strings inside a tabular cell
An Excel/CSV column (e.g. `metadata`, `payload`, `raw_response`) whose cells contain serialized JSON:
```json
{"patient_name": "John Doe", "contact": {"phone": "081234567890"}}
```
- Detect these columns (either by user declaration, or by sniffing whether values parse as JSON).
- Use **`Expr.str.json_decode()`** to parse into a `Struct` column, apply PII handling to matching nested fields, then re-serialize with **`Expr.struct.json_encode()`**.
- **Preserve the original serialization shape** on re-encode as closely as possible (key order, and don't reformat/pretty-print something that came in minified).

### Scenario B — Native nested JSON files
Input is `.json` or `.ndjson`/`.jsonl` with nested objects and arrays, where PII keys may live at **any depth**:
```json
{
  "member_code": "M-001",
  "patient": {
    "patient_name": "Jane Roe",
    "contacts": [
      {"email": "jane@example.com", "phone": "0812..."}
    ]
  }
}
```
- Use `pl.read_ndjson()` / `pl.scan_ndjson()` / `pl.read_json()`.
- PII detection must **recurse through `Struct` and `List[Struct]` types**, matching leaf field names against the PII list using the same normalized matching logic as flat columns.
- Handle arrays of objects (`List[Struct]`) — every element in the list must get consistent treatment.
- Use `unnest()` / `struct.field()` / `list.eval()` expressions rather than Python-side traversal wherever Polars supports it. Where the schema is too heterogeneous for native expressions, fall back to a **recursive Python traversal**, but isolate that in one clearly-marked module so the slow path is visible and testable.

### Scenario C — JSON path notation for user-registered columns
Users must be able to register nested targets by path, not just flat names:
```
patient.patient_name
patient.contacts[*].email
data.members[*].additional_member_code
```
- Support dotted paths and `[*]` wildcard for array elements.
- These paths work in the CLI (`--extra-pii-columns`), in the config file, and in the web UI.
- Also support the shorthand where a bare name like `email` means "match this leaf key **at any depth**" — make this the default behavior for bare names, since PII keys are often deeply buried. Provide `--strict-paths` to disable it if the user wants top-level-only matching.

### Schema Inference Caveats
- Polars infers JSON schema from a sample of rows by default; heterogeneous records will break this. Expose `--infer-schema-length N` (and allow `0`/`None` for full-file scan) and handle inference failures with a clear error rather than a stack trace.
- Fields present in some records and absent in others must not crash the run.
- Round-trip fidelity matters: nulls stay nulls, numeric types don't silently become strings, and a record that had no PII comes out byte-comparable where possible.

---

## User-Registered Custom Columns

- Users register additional column names/paths on top of the defaults via:
  - CLI flag: `--extra-pii-columns "ssn,dob,patient.contacts[*].email"`
  - Config file: `pii_config.yaml` in cwd or via `--config`
  - Web UI: a tag/text input before processing
- Custom entries **merge with** the defaults unless `--override-defaults` is passed.
- Persisted user config at `~/.pii_toolkit/config.yaml` so CLI users don't retype, managed via:
  - `pii-toolkit columns add <name-or-path>`
  - `pii-toolkit columns list`
  - `pii-toolkit columns remove <name-or-path>`

---

## Core Modes: Fake vs Redact

- **Default mode: `fake`** — synthesize realistic replacements that **preserve shape/type/format**: fake phones still look like phones, fake emails still parse as emails, fake card numbers match the original's length and delimiter pattern.
- **Alternate mode: `redact`** — configurable style: `full` (`[REDACTED]`), `partial` (`XXXX-XXXX-XXXX-1234`), or `token` (`REDACTED_001`).
- Use `Faker`, seeded per run, with a `--seed` flag for reproducibility in tests.
- **Consistency**: the same original value maps to the same replacement throughout a run — guaranteed structurally by the unique-values-first approach described above. This preserves groupings and joins in the faked data.

---

## Reversible Mapping (Critical Requirement)

- Each run produces a **mapping table** (original ↔ replacement), stored **separately** from the LLM-facing output and **never** embedded in it.
- Store as an encrypted or at-minimum access-controlled local file (`mapping_<timestamp>_<uuid>.json`, or SQLite for large mappings — SQLite is preferable once mappings exceed a few hundred thousand entries).
- Mapping entries should record the **source path/column** alongside the value pair, so restore can be scoped (avoid a fake name from one column clobbering an identical string in an unrelated column).
- Provide **restore/unmask** as both CLI command and Python API, accepting:
  - The LLM's output (tabular file, JSON, or free text)
  - The mapping file
  - Producing a restored version with originals substituted back.
- Restore must work when the LLM returns only a **subset** of data, or references faked values **inside free-form prose**. Implement via `str.replace_many()` (Aho–Corasick, single pass) rather than iterated regex.
- Restore must also handle **nested JSON output** — walking the returned structure and substituting at any depth.
- Mapping files expire: `pii-toolkit cleanup --older-than 7d`, since they contain real PII.

---

## File Format Support

- **Excel** (`.xlsx`, `.xls`) — via `pl.read_excel()` / `calamine`; multi-sheet aware. `--sheets "Sheet1,Sheet3"` or `--all-sheets`.
- **CSV / TSV** — via `scan_csv` for lazy streaming.
- **JSON / NDJSON / JSONL** — per the nested-JSON section above.
- **Parquet** — cheap to add given Polars, and the natural format for large data; include it.
- Auto-detect by extension, overridable by flag.

---

## CLI Design

Cross-platform (Linux/macOS terminal, Windows cmd.exe, PowerShell). Use **`typer`** for type-hint-driven ergonomics.

```bash
# Fake PII (default mode)
pii-toolkit process input.xlsx --output out_for_llm.xlsx --mapping-out mapping.json

# Redact instead
pii-toolkit process input.xlsx --mode redact --redact-style partial --output out.xlsx --mapping-out mapping.json

# Nested JSON with explicit paths
pii-toolkit process data.ndjson \
  --extra-pii-columns "patient.patient_name,patient.contacts[*].email" \
  --output out.ndjson --mapping-out mapping.json

# A tabular file with a JSON-string column
pii-toolkit process input.csv --json-columns "metadata,payload" --output out.csv --mapping-out mapping.json

# Restore
pii-toolkit restore llm_response.ndjson --mapping mapping.json --output restored.ndjson
pii-toolkit restore llm_response.txt --mapping mapping.json --output restored.txt

# Manage persisted custom PII list
pii-toolkit columns add ssn
pii-toolkit columns list
pii-toolkit columns remove ssn

# Cleanup old mappings
pii-toolkit cleanup --older-than 7d

# Serve web UI
pii-toolkit serve --port 8000
```

Requirements:
- Installs via `pip install .` / `pip install -e .`, exposing a console script through `[project.scripts]` in `pyproject.toml` that behaves identically on Windows/macOS/Linux.
- Good `--help` on every command.
- Clear errors: file not found, unsupported format, schema inference failure, **no PII columns detected** (warn + allow proceeding).
- Meaningful exit codes (0 success, non-zero failure) for pipeline automation.
- A `--dry-run` that reports which columns/paths *would* be transformed, with match counts, without writing output. This is important for trust before shipping data to an LLM.

---

## Simple Web App

Lightweight local app (**FastAPI + uvicorn**, minimal HTML/JS — no heavy frontend framework):

1. **File upload** (Excel/CSV/JSON/NDJSON/Parquet), drag-and-drop or picker.
2. **Detected PII preview** — show auto-detected columns *and nested paths* (rendered as a tree for nested structures) so the user can confirm/uncheck each.
3. **Custom input** — tag/text field for extra names or JSON paths.
4. **Mode toggle** — Fake (default) vs Redact, with redact-style sub-options.
5. **Process** — calls the *same core library functions as the CLI*. No logic duplication.
6. **Download result** — the LLM-safe file.
7. **Mapping handling** — downloadable separately and clearly labeled sensitive ("keep private, never send to the LLM"), or held server-side under a job ID for later restore.
8. **Restore tab** — upload the LLM's output (or paste text), supply mapping file or job ID, download restored result.
9. Runs fully **local, no internet, no external services**.
10. Security hygiene: never log PII values, clean up uploads and mappings after a configurable timeout, bind to `127.0.0.1` by default (not `0.0.0.0`) unless explicitly overridden.

---

## Architecture Requirements

Single core library; CLI and web are both thin wrappers over it.

```
pii_toolkit/
├── core/
│   ├── detector.py       # flat + nested path matching (exact/normalized/fuzzy)
│   ├── json_walker.py     # nested struct/list traversal, JSON path parsing, [*] wildcards
│   ├── generators.py      # category-aware fake generation, unique-values-first
│   ├── redactor.py        # full / partial / token styles
│   ├── mapper.py          # mapping build, persist (json/sqlite), lookup
│   ├── restorer.py        # replace_many-based restore for tabular, JSON, and free text
│   └── file_io.py         # excel/csv/ndjson/json/parquet via Polars
├── cli/
│   └── main.py            # typer app
├── web/
│   ├── app.py             # FastAPI
│   ├── templates/
│   └── static/
├── data/
│   └── default_pii_columns.yaml
├── tests/
├── pyproject.toml
└── README.md
```

Stack: **`polars`** (with `fastexcel` for Excel reads, `xlsxwriter` for Excel writes), `Faker`, `typer`, `fastapi` + `uvicorn`, `pydantic` for config models. **No pandas.**

Type hints throughout; docstrings on public API.

---

## Testing & Quality

`pytest` coverage for:
- Column detection — defaults, normalized matching, custom entries, fuzzy edge cases, no-match
- **Nested detection** — leaf keys at depth, `[*]` array wildcards, `List[Struct]`, mixed/absent fields across records
- **JSON-string-in-cell** round trip — decode → transform → re-encode preserves structure
- Fake consistency — same input → same output within a run; different seeds → different outputs
- Redaction styles
- **Mapping round trip** — fake → mapping → restore → originals recovered exactly, including nested
- Restore against partial responses and free-text prose
- Multi-sheet Excel
- Schema inference failure handling

Include small **synthetic** fixture files (never real PII), including a deeply nested NDJSON fixture.

Add a **benchmark script** (not a test) measuring throughput on a generated ~1M-row file, so the performance claims are verifiable and regressions are visible.

---

## Deliverables

1. Full scaffold matching the architecture above.
2. Working CLI (`process`, `restore`, `columns`, `cleanup`, `serve`, `--dry-run`), verified on Linux/macOS shells and Windows cmd/PowerShell — call out platform gotchas you handled (path handling, line endings, console encoding).
3. Working local web app: upload → configure → process → download, plus restore tab, with nested-path tree display.
4. `pyproject.toml` with console-script entry point and pinned dependencies.
5. Passing tests + the benchmark script.
6. `README.md` with install, CLI quickstart, web quickstart, JSON/nested-JSON examples, and a clear statement of the security model (where mappings live, what's in them, cleanup guidance).
7. Short note on limitations and next steps (encryption-at-rest for mappings, additional formats, multi-user deployment considerations).

Ask me clarifying questions if anything is ambiguous before starting; otherwise implement this as a complete, runnable project.
