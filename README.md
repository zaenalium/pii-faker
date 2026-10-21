# PII Faker

A Python toolkit for detecting, faking, or redacting Personally Identifiable Information (PII) in tabular and JSON datasets before sending them to Large Language Models (LLMs), and for putting the original values back afterwards.

## Features

- **PII detection by field name**: configurable matching (exact, normalized, fuzzy) on column names and nested JSON paths. Cell *values* are not inspected, see [Limitations](#limitations--next-steps)
- **Fake data generation**: realistic fakes that keep the format of the original (names, emails, phones, identifiers, addresses, dates). A value gets the same fake wherever it appears, and different values never share one
- **Redaction**: `full`, `partial` and `token` styles
- **Restore**: `fake` and `token` are lossless, so restoring returns your exact original data. `full` and `partial` are one-way
- **Multiple formats**: CSV/TSV, Excel (`.xlsx`), JSON, NDJSON, Parquet
- **Nested data**: struct and list columns, plus JSON documents stored as text in a cell
- **CLI & web UI**: the web UI requires an access token and deletes your files automatically
- **MCP server** for LLM providers you don't trust: the model only ever sees faked data, locally or through a tunnel
- **Fast on repetitive data**: builds the mapping from unique values only, then applies it with vectorised Polars operations

## Installation

Requires Python 3.10 or newer.

```bash
# Install from source
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"

# Install the MCP server (see "MCP Server")
pip install -e ".[mcp]"
```

## Quick Start

### Typical workflow

```bash
# 1. Fake the data and keep a private mapping
pii-faker process patients.csv --output patients_fake.csv --mapping-out mapping.json

# 2. Send patients_fake.csv to the LLM, save its answer as answer_fake.txt

# 3. Put the real values back into the answer
pii-faker restore answer_fake.txt --mapping mapping.json --output answer.txt --text
```

What step 1 does (real output of `--seed 42`; the fakes depend on your Faker version):

```
patient_id  patient_name  email                      phone     date_of_birth  note
P-1001      John Smith    john.smith@acme.com        555-0142  1984-03-12     Follow-up in 2 weeks
P-1002      Jane Doe      jane.doe@corp.org          555-0177  1991-11-02     Allergic to penicillin
P-1003      John Smith    john.smith@acme.com        555-0142  1984-03-12     Refill requested
                                         ↓
patient_id  patient_name  email                      phone     date_of_birth  note
P-1001      Noah Rhodes   jesseguzman@example.net    386-3794  2005-09-06     Follow-up in 2 weeks
P-1002      Allison Hill  garzaanthony@example.org  026-5423  1960-05-16     Allergic to penicillin
P-1003      Noah Rhodes   jesseguzman@example.net    386-3794  2005-09-06     Refill requested
```

A value gets the same fake wherever it appears, so `John Smith` is `Noah Rhodes` in both rows (and in any other name column too). Only columns whose **names** match are changed: `patient_id` and the free-text `note` above were left alone because `patient_id` is not a built-in name (add it with `-e patient_id`) and the contents of `note` are never scanned. Run with `--dry-run` first to see exactly what will be processed.

### CLI Usage

```bash
# Fake PII in a CSV file
pii-faker process data.csv --output processed.csv --mapping-out mapping.json

# Redact PII with partial style
pii-faker process data.xlsx --mode redact --redact-style partial --output redacted.xlsx --mapping-out mapping.json

# Process nested JSON with explicit paths
pii-faker process data.ndjson \
  --extra-pii-columns "patient.patient_name,patient.contacts[*].email" \
  --output processed.ndjson --mapping-out mapping.json

# Restore original values (tabular/JSON)
pii-faker restore processed.csv --mapping mapping.json --output restored.csv

# Restore free-form text (e.g. LLM prose referencing faked values)
pii-faker restore llm_response.txt --mapping mapping.json --output restored.txt --text

# Manage custom PII columns (saved in ~/.pii_faker/config.yaml, applied by every run)
pii-faker columns add ssn
pii-faker columns list
pii-faker columns remove ssn

# Preview what would be processed without writing output
pii-faker process data.csv --output processed.csv --dry-run

# Reproducible fakes + custom matching
pii-faker process data.csv --output processed.csv --mapping-out mapping.json \
  --seed 42 --match-strategy fuzzy --extra-pii-columns "ssn,dob" --override-defaults

# Excel: the first sheet by default. Several sheets are written one file per
# sheet (processed_Sheet1.xlsx, processed_Sheet3.xlsx), all sharing one mapping
pii-faker process data.xlsx --output processed.xlsx --mapping-out mapping.json \
  --sheets "Sheet1,Sheet3"
pii-faker process data.xlsx --output processed.xlsx --mapping-out mapping.json --all-sheets
pii-faker restore processed_Sheet1.xlsx --mapping mapping.json --output restored_Sheet1.xlsx

# JSON/NDJSON infer their schema from the first 100 rows, so a field that first
# appears later is missed. 0 reads every row
pii-faker process data.ndjson --output processed.ndjson --mapping-out mapping.json \
  --infer-schema-length 0

# A .db/.sqlite extension writes the mapping as SQLite instead of JSON
pii-faker process big.csv --output processed.csv --mapping-out mapping.db

# Start the web UI (prints a sign-in link)
pii-faker serve --port 8000

# Delete old mapping files: mapping*.json, mapping*.db, mapping*.sqlite
pii-faker cleanup --older-than 7d
```

> **Keep `--mapping-out`.** Without it nothing is saved and fakes cannot be restored; the CLI prints a warning. The mapping is written *before* the output, so if it cannot be saved no output file is produced.

#### `process` options

| Option | Meaning |
|---|---|
| `-o, --output` | Output file (required) |
| `-m, --mapping-out` | Where to write the mapping (`.json`, or `.db`/`.sqlite` for SQLite) |
| `--mode` | `fake` (default) or `redact` |
| `--redact-style` | `full` (`[REDACTED]`), `partial` (last 4 characters kept), `token` (`REDACTED_0001`) |
| `--match-strategy` | `exact`, `normalized` (default) or `fuzzy` |
| `-s, --seed` | Seed for reproducible fakes |
| `-e, --extra-pii-columns` | Comma-separated extra names or paths |
| `--override-defaults` | Ignore the built-in column names |
| `-j, --json-columns` | Columns holding JSON documents as text |
| `--strict-paths` | Bare names match top-level fields only |
| `--sheets`, `--all-sheets` | Excel sheets to process |
| `--infer-schema-length` | Rows used to infer a JSON schema (default 100, `0` = all) |
| `--dry-run` | Report what would be processed, write nothing |

If no PII is detected the CLI asks whether to continue, which blocks non-interactive runs. Use `--dry-run` in scripts to check first.

#### `serve` options

| Option | Meaning |
|---|---|
| `--host` | Address to bind (default `127.0.0.1`) |
| `-p, --port` | Port (default `8000`) |
| `--token` | Access token, at least 16 characters. Prefer the `PII_FAKER_TOKEN` environment variable: command lines are visible to other users. A random token is generated if you give neither |
| `--retention-minutes` | Delete a job's files after this long without activity (default `60`) |

#### `cleanup` options

| Option | Meaning |
|---|---|
| `--older-than` | Age limit, such as `7d` or `24h` (default `7d`) |
| `-d, --directory` | Folder to clean (default `~/.pii_faker/mappings`) |

### Python API

```python
import polars as pl
from pii_faker.core import process_dataframe, restore_dataframe, PIIConfig
from pii_faker.core.mapper import PIIMapper

# Load data
df = pl.read_csv("data.csv")

# Configure
config = PIIConfig(
    mode="fake",
    seed=42,
    extra_pii_columns=["ssn", "dob"],
)

# Process
processed_df, mapper = process_dataframe(df, config, mapping_out="mapping.json")

# Later, restore original values
mapper = PIIMapper.load("mapping.json")
restored_df = restore_dataframe(processed_df, mapper)
```

`process_dataframe` handles flat columns, nested struct/list columns and (with `json_columns=[...]` in the config) JSON documents stored as text. To process several frames, such as the sheets of a workbook, with one shared mapping, reuse a `PIIProcessor`:

```python
from pii_faker.core import PIIProcessor

processor = PIIProcessor(PIIConfig(seed=42))
fake_sheet1 = processor.process(sheet1)
fake_sheet2 = processor.process(sheet2)      # shared values get the same fake
processor.mapper.save("mapping.json")
print(processor.skipped)                     # matched by name but held no text
```

> **What can be restored.** `fake` mode and `redact --redact-style token` are lossless: restoring returns the exact original values, and a mapping that could not do so (two originals sharing one replacement) is refused when saving. `redact` with `full` (`[REDACTED]`) or `partial` is one-way, because many originals collapse to the same placeholder.

### Web UI

```bash
pii-faker serve --port 8000
```

The server prints a sign-in link containing this run's access token:

```
Open this link to sign in: http://127.0.0.1:8000/?token=...
```

Open it in your browser (it signs you in and removes the token from the address bar), then:
1. Upload files
2. Preview detected PII columns
3. Select columns to process
4. Choose fake or redact mode
5. Download processed files and mapping

To use a token of your own, set `PII_FAKER_TOKEN` (at least 16 characters) before starting; the link is then not printed and you sign in by typing the token. Scripts can skip the browser and send `Authorization: Bearer <token>`. See [Web UI security](#web-ui-security) for what is and isn't protected.

## MCP Server: Using an LLM Provider You Don't Trust

Everything an MCP tool returns is sent to your LLM provider, so an ordinary MCP server that reads your files hands them over. `pii-faker mcp` fakes the data **once, at startup**, and every tool only reads that faked copy. The mapping back to the real values stays on your machine and no tool can return it.

```bash
pip install -e ".[mcp]"

# 1. Look at what the model would be able to see. Nothing is served
pii-faker mcp --dataset patients=patients.csv --keep age,visits --dry-run

# 2. Serve it. Your MCP client launches this command (stdio)
pii-faker mcp --dataset patients=patients.csv --keep age,visits \
  --mapping-out private/mapping.json --restore-dir private/restored
```

Most MCP clients take a configuration like this (see your client's documentation for where it goes):

```json
{
  "mcpServers": {
    "pii-faker": {
      "command": "pii-faker",
      "args": ["mcp", "--dataset", "patients=/data/patients.csv", "--keep", "age,visits",
               "--mapping-out", "/data/private/mapping.json"]
    }
  }
}
```

### What the model can see

**Columns are an allowlist.** A column, nested field or JSON key reaches the model only if it was faked or you named it in `--keep`. Anything the detector did not recognise is **dropped**, and so is a column that looks like PII but holds no text (a phone stored as a number). This is the opposite of `pii-faker process`, which passes unrecognised columns through, and it is why an untrusted provider needs a different tool.

```
$ pii-faker mcp --dataset sample=sample.csv --dry-run
 Column / field  Exposed as
 date_of_birth   faked
 email           faked
 patient_name    faked
 phone           faked
 patient_id      dropped
 note            dropped
```

The report lists column names only, never values. If you need a column the detector doesn't know, name it with `-e` to fake it or `--keep` to show it unchanged. `--keep-unmatched` exposes every unrecognised column as-is; it is there for data you've already vetted and is not recommended for an untrusted provider.

### Tools

All read-only, all working on the faked copy, and all row-capped (`--max-rows`, default 100).

| Tool | What it does |
|---|---|
| `list_datasets` | Dataset aliases with row and column counts |
| `describe_dataset` | Columns, types, whether each is faked, null/distinct counts, a 5-row sample |
| `read_rows` | A page of rows, optionally some columns |
| `search_rows` | Rows where a column equals or contains a value (matches the *faked* values) |
| `aggregate` | Group by columns and compute count, n_unique, sum, mean, min, max |
| `restore_to_file` | Only with `--restore-dir`: puts real values back into text that quotes fakes and saves it **on your machine**. The model gets back a file name and a count, never the text |

The model never supplies a file path: datasets are the aliases you registered with `--dataset name=path`. It is told that the data is synthetic and to quote values exactly, so that you can map its answers back.

### Restoring the model's answers

Either let the model call `restore_to_file` (the restored text lands in `--restore-dir`; never expose that folder to the model), or restore afterwards yourself with the mapping you saved:

```bash
pii-faker restore answer_fake.txt --mapping private/mapping.json --output answer.txt --text
```

`restore` only finds fakes the model repeated verbatim; a paraphrase like "Mr. Rhodes" is not restored. Without `--mapping-out` or `--restore-dir` the real values cannot be recovered after the server stops, and it warns you.

### `mcp` options

| Option | Meaning |
|---|---|
| `-d, --dataset name=path` | A dataset to serve (repeat for several). Excel uses the first sheet |
| `--keep a,b.c,col:json.key` | Expose these columns, nested paths or JSON keys with their original values |
| `--keep-unmatched` | Expose every unrecognised column as-is (not recommended) |
| `-e`, `-j`, `--match-strategy`, `--override-defaults`, `--strict-paths`, `--seed` | As for `process`; saved custom columns apply too |
| `--mode`, `--redact-style` | `fake` (default), or `redact` with `full` / `token`. `partial` is refused: it leaks characters |
| `-m, --mapping-out` | Save the real↔fake mapping to a private (`0600`) file |
| `--restore-dir` | Enable `restore_to_file`, saving into this folder |
| `--max-rows` | Most rows a tool call returns (default 100) |
| `--transport` | `stdio` (default) or `http` |
| `--host`, `-p/--port` | `http` only: bind address (default `127.0.0.1`) and port (default `8765`) |
| `--token` | `http` only: access token, at least 16 characters. Prefer the `PII_FAKER_MCP_TOKEN` environment variable. Generated and printed if omitted |
| `--allowed-host` | `http` only: a hostname the server may be reached by, such as your tunnel's (repeatable) |
| `--dry-run` | Show what the model would see, then exit |

### Using it remotely

Where the server runs decides where the real data and mapping live. The model (and your provider) only ever sees the faked copy, wherever it is served from.

**1. Client and data on the same machine.** Use stdio, as above. No network is involved.

**2. Data on a remote machine, client on yours.** Run the server over SSH, so the client talks stdio through the connection and no port is opened:

```json
{ "mcpServers": { "pii-faker": {
    "command": "ssh",
    "args": ["-T", "me@data-host", "pii-faker", "mcp", "--dataset", "patients=/data/patients.csv",
             "--keep", "age,visits", "--mapping-out", "/data/private/mapping.json"]
} } }
```

Or serve HTTP on the remote machine and forward the port, which keeps it off the network: `ssh -L 8765:127.0.0.1:8765 me@data-host`, then point the client at `http://127.0.0.1:8765/mcp`.

**3. A client that can only reach a public HTTPS address (a hosted agent platform, say).** Serve HTTP and put a tunnel in front:

```bash
# start the tunnel first, so you know its hostname
cloudflared tunnel --url http://127.0.0.1:8765        # or: ngrok http 8765

export PII_FAKER_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
pii-faker mcp --dataset patients=patients.csv --keep age,visits \
  --transport http --port 8765 --allowed-host abc-123.trycloudflare.com \
  --mapping-out private/mapping.json
```

Give the client the URL `https://abc-123.trycloudflare.com/mcp` and the header `Authorization: Bearer <your token>`. Without `--allowed-host` the server answers `421 Invalid Host header`: it checks the `Host` and `Origin` headers to defeat DNS-rebinding, and your tunnel's hostname is not one it expects.

What a tunnel does and doesn't change:

- **The real data and the mapping stay on your machine.** The tunnel carries only faked, pruned data and the token.
- **The tunnel provider can read that traffic** (it terminates TLS), and so can your LLM provider. Both see only the faked copy, but treat the token as a secret and use a fresh one per session.
- **Every request needs the token**, compared in constant time, before anything else happens. The server does not rate-limit, which is why a token you supply must be 16+ characters.
- **Prefer a private network when you can.** Tailscale (`tailscale serve`) keeps the endpoint inside your tailnet instead of on the public internet. Cloudflare Access or an ngrok access policy add a second lock in front of the token.
- **Your client must be able to send an `Authorization` header.** Connectors that support only OAuth sign-in cannot use this; this server does not implement OAuth.
- The server listens on `127.0.0.1` only unless you pass `--host`, and warns if you expose it over plain HTTP.

The HTTP transport, token check and host check are covered by tests against a real server and client, and the command was exercised as a separate process. I have not run it through cloudflared, ngrok, Tailscale or an SSH session, so check those steps against their own documentation.

### What this does not protect against

- **Pseudonymised is not anonymous.** Columns you `--keep` and any quasi-identifiers in them (a rare diagnosis with a postcode and birth year) can still identify people.
- **Relationships are preserved on purpose.** The same person has the same fake in every row, column and dataset, so linkage, frequencies and the number of distinct values are visible. Row order is preserved too: a file sorted by real surname shows its fake names in that same order.
- **Fake dates and ids carry no real statistics.** Keep a derived column (an `age`) if the model needs one.
- **The model can page through everything exposed.** There is no per-session budget; stop the server when you are done.
- **Detection is by name.** That is why unrecognised columns are dropped. Free-text columns are only shown if you `--keep` them, with whatever PII they contain.
- **Don't attach other raw-data MCP servers to the same session.** One tool that reads the originals defeats all of this.
- Everything is held in memory (limits as for `process`), and the datasets are fixed when the server starts.

## JSON & Nested JSON Support

### Scenario A: JSON Strings in Tabular Cells

```bash
pii-faker process data.csv \
  --json-columns "metadata,payload" \
  --output processed.csv --mapping-out mapping.json
```

Each distinct document is parsed once and PII is found by key name using the same rules as below. Values that are not valid JSON, and nulls, pass through unchanged. A document that changes is re-written compactly (no spaces after `:` and `,`); documents that don't change keep their exact original text. Mapping keys look like `payload:contact.phone`.

### Scenario B: Native Nested JSON

For NDJSON and JSON files (and Parquet struct columns):

```bash
pii-faker process data.ndjson \
  --extra-pii-columns "patient.patient_name,patient.contacts[*].email" \
  --output processed.ndjson --mapping-out mapping.json
```

Nulls, empty lists and every field that isn't PII are left exactly as they were. Mapping keys are the paths, such as `patient.contacts[*].email`.

### Scenario C: JSON Path Notation

```
patient.patient_name
patient.contacts[*].email
data.members[*].additional_member_code
```

- A **bare name** (`email`) matches a field of that name at any depth. `--strict-paths` limits this to top-level fields (for a JSON cell, the document's root keys).
- A **path** (anything containing `.` or `[`) matches that one field. It applies even with `--strict-paths`, and `[*]` is optional: `patient.contacts.email` and `patient.contacts[*].email` are the same.
- A match on a **container** covers everything inside it: an `address` struct with `street` and `city` fields has both processed.
- Only text is changed. A field that matches by name but holds numbers (a phone stored as an integer, say) is **left unchanged and the CLI warns** about it.
- A path that matches nothing is not reported. Check with `--dry-run`.
- Top-level column names may contain dots, but nested field names containing `.` or `[*]` are not supported.

## Configuration

### Default PII Columns

Matched by column or field name (case, spaces, `_` and `-` are ignored by default):

| Category | Names |
|---|---|
| name | `name`, `patient_name`, `member_name` |
| identifier | `patientcardnumber`, `card_number`, `card_no`, `member_code`, `additional_member_code` |
| contact | `phone`, `store_phone`, `email` |
| address | `address` |
| date | `date_of_birth`, `birth_date`, `dob` |

The category decides the kind of fake: identifiers and phone numbers keep their length and punctuation with the digits randomised, dates keep their separator and field order (and are adults aged 18-90), and so on. Identifiers keep their shape (length, case, punctuation) but **both digits and letters are randomised**, so `M-001` becomes something like `K-482` and `ABCDEF` something like `ESZYCI`.

Anything you add yourself gets the `custom` category: values containing digits keep their shape (digits and letters randomised, punctuation kept) and other text becomes a random word. A path you add whose last field is a built-in name (`patient.patient_name`) takes that name's category instead.

### Custom Columns

```bash
# Add custom columns
pii-faker columns add ssn
pii-faker columns add "patient.contacts[*].phone"

# List all custom columns
pii-faker columns list

# Remove a column
pii-faker columns remove ssn
```

Custom columns are stored in `~/.pii_faker/config.yaml` and applied by every `process` run, in addition to anything passed with `--extra-pii-columns`. (`columns --config FILE` selects a different file for that command only; `process` always reads the default one.)

### Match Strategies

- **exact**: Column name must match exactly (case-sensitive)
- **normalized** (default): Case-insensitive, ignores spaces/underscores/hyphens
- **fuzzy**: Substring and similarity matching. Expect false positives on unrelated columns

## File Formats

| Format | Read | Write | Notes |
|--------|------|-------|-------|
| CSV/TSV | ✓ | ✓ | |
| Excel (`.xlsx`) | ✓ | ✓ | Uses calamine (fastexcel) for reads. One output file per processed sheet |
| JSON | ✓ | ✓ | Schema inference configurable |
| NDJSON/JSONL | ✓ | ✓ | Schema inference configurable |
| Parquet | ✓ | ✓ | Best for large datasets |

Files are read fully into memory.

## Security Model

### Where Mappings Live

- Mappings are written **only** where you point `--mapping-out`. There is no default location
- On Unix they are created readable by the owner only (mode `0600`), including when you overwrite an existing, more open file
- Nothing in the package makes network requests; a mapping is only ever sent to an LLM if you send it
- JSON (default) or SQLite (`.db`/`.sqlite` extension). Both are loaded fully into memory to restore, and in our measurements JSON was smaller and faster (1M entries: 36 MB, 0.9 s to load, versus 59 MB and 2.1 s for SQLite). Choose SQLite if you want to inspect the mapping with other tools

### What's in Mappings

- Original → replacement value pairs, grouped by column or path, with each one's category
- Processing mode, redact style, match strategy and **seed** (if you gave one)
- Creation timestamp

### Cleanup Guidance

```bash
# Delete mapping files older than 7 days
pii-faker cleanup --older-than 7d

# Delete mapping files older than 24 hours
pii-faker cleanup --older-than 24h

# Specify custom directory (default: ~/.pii_faker/mappings)
pii-faker cleanup --older-than 7d --directory /path/to/mappings
```

`cleanup` deletes files in that one folder whose name starts with `mapping` and ends in `.json`, `.db` or `.sqlite` (`mapping.json`, `mapping_2026-10-21_patients.json`, `mapping.db`, ...) and reports how many it removed. Anything else, such as `mapping.py` or `notes.json`, is never touched. Age comes from a date in the file name (`mapping_<YYYY-MM-DD>_...`) when there is one, otherwise from the file's modified time.

**Important**: Mapping files contain real PII. Treat them as sensitive data.

### Web UI security

- **Token required.** Every route except the sign-in page needs the access token (a random 256-bit value unless you set your own). A browser exchanges it once for a session cookie (`HttpOnly`, `SameSite=Strict`, 8 hours) and the token is not kept in the browser. Responses are marked `no-store`
- **One shared token, no accounts.** Everyone holding it can see every job. It suits a single user or a small trusted team, not a multi-tenant service
- **No rate limiting.** That is why a token you supply must be at least 16 characters
- **Plain HTTP.** The server binds to `127.0.0.1` by default. If you listen on a network address, the token and your data travel unencrypted unless you put a TLS-terminating proxy in front; the server warns when you do
- **Files are temporary.** Uploads, outputs and mappings live in a private (`0700`) temp directory. A job's files are deleted after `--retention-minutes` (default 60) without activity, on request with `DELETE /api/jobs/<job id>`, and all at once when the server stops. If the process is killed outright, remove the leftover `pii_faker_web_*` folder from your temp directory yourself
- Jobs and sessions are held in memory by a single process, so restarting the server signs everyone out and drops every job

### What this does *not* protect against

- **Anything not matched by name.** Detection reads column and field names only, so PII inside free-text cells, or in columns with unrecognised names, is sent through untouched
- **Quasi-identifiers.** Combinations such as postcode + birth year + rare diagnosis can re-identify people even when every named column is faked
- **Look-alike fakes.** A fake name can coincide with a real person
- **`partial` redaction** deliberately keeps the last 4 characters (`XXXXXXmith`, `XXXX1234`)

## Performance

### Unique-Values-First Approach

The toolkit builds mappings only for unique values, then applies them vectorised:

- 5M rows with 40k distinct names → ~125x fewer Faker calls
- A value gets the same fake wherever it appears: in any column of the same kind (a person in `patient_name` and `member_name`), in nested fields and JSON cells, and across the sheets of one workbook. Different values never share a fake
- Polars `replace()` for vectorised application

Cost therefore follows the number of **distinct** values, not rows. Measured on one Linux machine (Python 3.14, Polars 1.43):

| Case | Time |
|---|---|
| 1M rows, two columns of ~1,000 distinct values | 0.4 s to process, 0.05 s to restore |
| 20,000 distinct names in one column | 3 s (~5-7k distinct values/s) |
| 100,000 distinct names in one column | 22 s |
| Restoring a 100k-entry mapping onto a column | 0.2 s |
| `restore --text`, 100k-entry mapping, 2 KB / 50 KB of text | ~0.1 s / ~4 s |

Free-text restore does one find-and-replace per mapping entry, so its time grows with mapping size × text length.

### Benchmarks

```bash
python -m pii_faker.tests.benchmark
```

The benchmark uses about 1,000 distinct values, which is the favourable case above.

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=pii_faker

# Run specific test file
pytest pii_faker/tests/test_core.py
```

| File | Covers |
|---|---|
| `test_core.py` | Unit tests for detection, generation, redaction, mapping, restore, file I/O |
| `test_reversibility.py` | Fake uniqueness at scale, identifiers, cross-column consistency, exact round trips, mapping files, `cleanup` |
| `test_nested.py` | Struct/list columns and JSON documents in cells |
| `test_cli.py` | The CLI examples in this README, end to end, including `serve` and `cleanup` |
| `test_web.py` | Web UI sign-in and token checks, job expiry and cleanup, detect → process → restore |
| `test_mcp.py` | MCP gateway: what is exposed, that no tool returns a real value (canary test), error handling, restore tool, HTTP auth and host checks |

## Limitations & Next Steps

### Limitations

- **Detection is name-based.** Free-text columns are never scanned for PII
- A PII column stored as numbers (a phone read as an integer) is not changed; the CLI warns
- Fuzzy matching may produce false positives on unrelated columns
- Schema inference samples the first 100 rows of JSON/NDJSON by default; use `--infer-schema-length 0` if fields can first appear later
- Files are read fully into memory. Excel is the slowest format, so prefer CSV/NDJSON for large exports
- `redact --redact-style full/partial` is not reversible (many-to-one placeholders); use `fake` or `token` when lossless restore matters
- A format-preserving fake space can run out (for example more than 10,000 distinct 4-digit codes in one column); the run then stops with an error suggesting `token` redaction
- `restore --text` is a plain find-and-replace of known fake values, so it works best when fakes are distinctive
- Web UI: one shared access token (no per-user accounts), plain HTTP, jobs held in memory by a single process, and its own detection code alongside the CLI's

### Next Steps

- Encryption-at-rest for mapping files
- Value-based detection (patterns or NER) for free-text columns
- Streaming support for very large files
- A `--yes` flag so runs with no detected PII don't prompt
- Web UI: TLS option and per-user accounts for multi-user deployments

## License

MIT License

## Contributing

Contributions welcome! Open an issue or pull request on GitHub.
