# PII Faker

A Python toolkit for detecting, faking, or redacting Personally Identifiable Information (PII) in tabular and JSON datasets before sending them to Large Language Models (LLMs).

## Features

- **PII Detection**: Automatically detects PII columns by name AND by value shape (emails, phones, credit cards, NIK/NPWP/BPJS, …), with confidence scores
- **Leak check**: Re-scans processed output for residual PII and warns
- **Fake Data Generation**: Generates realistic fake data preserving format and structure
- **Redaction**: Multiple redaction styles (full, partial, token, typed-token)
- **Reversible Mapping**: All transformations are reversible via mapping files (v2 format with native types, per-sheet scope, and session ID)
- **Multiple Formats**: Supports CSV, Excel, JSON, NDJSON, Parquet
- **Nested JSON**: Full support for nested structures and JSON paths
- **CLI & Web UI**: Command-line interface and web-based interface. The web UI requires an access token and deletes your files after an idle period
- **MCP server** for LLM providers you don't trust: the model only ever sees faked data, locally or through a tunnel (see [MCP Server](#mcp-server-using-an-llm-provider-you-dont-trust))
- **High Performance**: Uses Polars for vectorized operations and unique-values-first approach

## Installation

```bash
# Install from source
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"

# Install for the macOS desktop app (needs a WebView)
pip install -e ".[desktop]"

# Install with encryption support (AES-GCM + keychain)
pip install -e ".[enc]"

# Install the MCP server (see "MCP Server")
pip install -e ".[mcp]"
```

## Desktop App (macOS today, Windows next)

```bash
# Launch from source
pii-faker-desktop
python -m pii_faker.desktop

# Or build an unsigned .app/.dmg (no Apple Developer ID needed)
pip install pyinstaller pywebview platformdirs
pyinstaller --clean --noconfirm packaging/pii-faker-mac.spec
# → dist/PII Faker.app, dist/PII-Faker-mac-arm64.dmg
```

The desktop app runs the same FastAPI UI in a native window on
`127.0.0.1` with a random port and a per-launch session token, so other
local processes cannot call its API. It supports both upload and
**path-based processing** (native file dialog, no browser upload copy —
best for large files), plus drag-and-drop. The server shuts down when
you close the window.

> Unsigned builds show macOS Gatekeeper / Windows SmartScreen warnings
> until 1.8 code signing is provisioned (Apple Developer ID / Azure
> Trusted Signing). WebView2 is built into Windows 11 and updated
> Windows 10 (1.9).

## Quick Start

### CLI Usage

```bash
# Fake PII in a CSV file
pii-faker process data.csv --output processed.csv --mapping-out mapping.json

# Encrypted mapping (AES-GCM — passphrase, env var, or OS keychain)
pii-faker process data.csv --output processed.csv --mapping-out mapping.json --encrypt --passphrase s3cret
pii-faker key set          # store a master key in the OS keychain
pii-faker key show         # check whether a key exists (never prints it)

# Indonesia preset (UU PDP: NIK/NPWP/BPJS + id_ID Faker locale)
pii-faker process data.csv --preset id --output processed.csv --mapping-out mapping.json
pii-faker presets list

# PII-free audit next to the mapping (JSON + human text)
pii-faker process data.csv --output processed.csv --mapping-out mapping.json --audit-out audit.json

# Redact PII with partial style
pii-faker process data.xlsx --mode redact --redact-style partial --output redacted.xlsx --mapping-out mapping.json

# Redact with globally unique typed tokens (best default before an LLM)
pii-faker process data.csv --mode redact --redact-style typed-token --output tok.csv --mapping-out mapping.json

# Process nested JSON with explicit paths
pii-faker process data.ndjson \
  --extra-pii-columns "patient.patient_name,patient.contacts[*].email" \
  --output processed.ndjson --mapping-out mapping.json

# Restore original values (tabular/JSON)
pii-faker restore processed.csv --mapping mapping.json --output restored.csv

# Restore free-form text (e.g. LLM prose referencing faked values)
pii-faker restore llm_response.txt --mapping mapping.json --output restored.txt --text

# Manage custom PII columns
pii-faker columns add ssn
pii-faker columns list
pii-faker columns remove ssn

# Preview what would be processed without writing output
pii-faker process data.csv --output processed.csv --dry-run

# Reproducible fakes + custom matching
pii-faker process data.csv --output processed.csv --mapping-out mapping.json \
  --seed 42 --match-strategy fuzzy --extra-pii-columns "ssn,dob" --override-defaults

# Indonesian data with localised fakes
pii-faker process data.csv --output processed.csv --mapping-out mapping.json \
  --locale id_ID

# Excel sheets and JSON schema sampling
pii-faker process data.xlsx --output processed.xlsx --mapping-out mapping.json \
  --sheets "Sheet1,Sheet3"
pii-faker process data.ndjson --output processed.ndjson --mapping-out mapping.json \
  --infer-schema-length 0

# SQLite mapping for large runs (use .db/.sqlite extension)
pii-faker process big.csv --output processed.csv --mapping-out mapping.db

# Start web UI
pii-faker serve --port 8000

# Cleanup old mappings
pii-faker cleanup --older-than 7d
```

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

> Note: `fake` mode is fully reversible. `redact` mode with `full` (`[REDACTED]`)
> or `partial` styles is lossy — many originals collapse to one placeholder, so
> exact restore is only guaranteed for `fake` and `token` styles.

### Web UI

```bash
pii-faker serve --port 8000
```

The server prints a sign-in link containing this run's access token:

```
Open this link to sign in: http://127.0.0.1:8000/?token=...
```

Open it in your browser (it signs you in and removes the token from the address bar). To use a token of your own, set `PII_FAKER_TOKEN` (at least 16 characters) before starting; the link is then not printed and you sign in by typing the token. Scripts can send `Authorization: Bearer <token>`. Jobs (uploads, outputs, mappings) are deleted after `--retention-minutes` (default 60) without activity, with `DELETE /api/jobs/<job id>`, and when the server stops. See [Web UI security](#web-ui-security).

Once signed in:
1. Upload files (500 MB cap; filenames are sanitised)
2. Preview detected PII columns (flat, nested, and JSON-string leaves)
3. Select columns to process (Redact + typed-token is the LLM-safe default)
4. Download processed files, mapping, and PII-free audit
5. **LLM Round-trip tab**: paste the model's response, get restored text with highlighted spans

### LLM round-trip workflow

```bash
# Process with typed tokens (models rarely rewrite <PERSON_1>, but often rewrite "Mr. Smith")
pii-faker process data.csv --mode redact --output tok.csv --mapping-out mapping.json

# Paste the LLM response into the Round-trip tab, or restore pasted text via the API.
# Tolerant restore handles case changes, possessives ("Smith's") and
# first/last-name-only mentions; ambiguous fragments are left alone.

# Date shifting (one consistent offset per run, intervals preserved)
pii-faker process data.csv --shift-days 17 --output shifted.csv --mapping-out mapping.json

# Numeric perturbation (±percent noise, deterministic per value)
pii-faker process data.csv --perturb-pct 5 --output pert.csv --mapping-out mapping.json

# Project mode: same person → same fake across files
pii-faker project init myproject --mapping proj.json
pii-faker process a.csv --output a_out.csv --project-mapping proj.json --seed 1
pii-faker process b.csv --output b_out.csv --project-mapping proj.json --seed 1

# Local LLM proxy (Phase 5, exploratory): OpenAI-compatible scrub + restore on 127.0.0.1
pii-faker proxy --upstream https://api.openai.com --port 11434
# Point any OpenAI client at http://127.0.0.1:11434/v1
# Scrub is deterministic detectors only (email/phone/NIK/BPJS/card/IBAN/IP/URL-creds) — prose names pass through.
```

> Note: the web UI runs on localhost only and is designed for a single
> trusted user. Uploaded originals and plaintext mappings live in a temp
> directory until you download the results, then are deleted.

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
      "command": "/full/path/to/pii-faker",
      "args": ["mcp", "--dataset", "patients=/data/patients.csv", "--keep", "age,visits",
               "--seed", "7", "--mapping-out", "/data/private/mapping.json"]
    }
  }
}
```

Use the full path from `which pii-faker`: desktop apps don't inherit your shell's `PATH`. Keep `--seed`: without it the fakes change every time the app starts the server, and the mapping file is overwritten, so earlier answers can no longer be restored.

### What the model can see

**Columns are an allowlist.** A column, nested field or JSON key reaches the model only if it was faked or you named it in `--keep`. Anything the detector did not recognise is **dropped**, and so is PII the pipeline could not process (an unsupported column type) or only partly handled (free text). Structures the detector cannot see into, such as a list of lists, are dropped too, never shown as-is. This is the opposite of `pii-faker process`, which passes unrecognised columns through, and it is why an untrusted provider needs a different tool.

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

The report lists column names only, never values. Columns are recognised by name and by the shape of their values, exactly as for `process`. If you need a column the detector doesn't know, name it with `-e` to fake it or `--keep` to show it unchanged. `--keep-unmatched` exposes every unrecognised column as-is; it is there for data you've already vetted and is not recommended for an untrusted provider.

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

Only fakes the model repeated verbatim are found; a paraphrase like "Mr. Rhodes" is not restored. Without `--mapping-out` or `--restore-dir` the real values cannot be recovered after the server stops, and it warns you.

### `mcp` options

| Option | Meaning |
|---|---|
| `-d, --dataset name=path` | A dataset to serve (repeat for several). Excel uses the first sheet |
| `--keep a,b.c,col:json.key` | Expose these columns, nested paths or JSON keys with their original values |
| `--keep-unmatched` | Expose every unrecognised column as-is (not recommended) |
| `-e`, `-j`, `--match-strategy`, `--override-defaults`, `--strict-paths`, `--seed`, `--locale`, `--preset` | As for `process`; saved custom columns apply too |
| `--mode`, `--redact-style` | `fake` (default), or `redact` with `typed-token` (default), `token` or `full`. `partial`, `shift` and `perturb` are refused: they leave identifying values visible |
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

**2. Data on a remote machine, client on yours.** Run the server over SSH, so the client talks stdio through the connection and no port is opened (log in with a key: an AI app can't type a password):

```json
{ "mcpServers": { "pii-faker": {
    "command": "ssh",
    "args": ["-T", "me@data-host", "/home/me/venv/bin/pii-faker", "mcp", "--dataset", "patients=/srv/pii/patients.csv",
             "--keep", "age,visits", "--seed", "7", "--mapping-out", "/srv/pii/private/mapping.json"]
} } }
```

`ssh` joins these arguments and the remote shell parses them again, so a path with a space needs quotes inside the string: `"patients='/srv/my data/patients.csv'"`. Or serve HTTP on the remote machine and forward the port, which keeps it off the network: `ssh -L 8765:127.0.0.1:8765 me@data-host`, then point the client at `http://127.0.0.1:8765/mcp`.

**3. A client that can only reach a public HTTPS address (a hosted agent platform, say).** Serve HTTP and put a tunnel in front:

```bash
# start the tunnel first, so you know its hostname
cloudflared tunnel --url http://127.0.0.1:8765        # or: ngrok http 8765

export PII_FAKER_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
pii-faker mcp --dataset patients=patients.csv --keep age,visits --seed 7 \
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

The HTTP transport, token check and host check are covered by tests against a real server and client, and the command was exercised as a separate process. The SSH configuration was exercised with a stand-in that handles arguments the way `ssh` does, not a real SSH session, and I have not run it through cloudflared, ngrok or Tailscale, so check those steps against their own documentation.

### What this does not protect against

- **Pseudonymised is not anonymous.** Columns you `--keep` and any quasi-identifiers in them (a rare diagnosis with a postcode and birth year) can still identify people.
- **Relationships are preserved on purpose.** The same person has the same fake in every row, column and dataset, so linkage, frequencies and the number of distinct values are visible. Row order is preserved too: a file sorted by real surname shows its fake names in that same order.
- **Fake dates and ids carry no real statistics.** Keep a derived column (an `age`) if the model needs one.
- **The model can page through everything exposed.** There is no per-session budget; stop the server when you are done.
- **Detection is by name and value shape.** That is why unrecognised columns are dropped. Free-text columns are only shown if you `--keep` them, with whatever PII they contain.
- **Don't attach other raw-data MCP servers to the same session.** One tool that reads the originals defeats all of this.
- Everything is held in memory (limits as for `process`), and the datasets are fixed when the server starts.

## JSON & Nested JSON Support

### Scenario A: JSON Strings in Tabular Cells

```bash
pii-faker process data.csv \
  --json-columns "metadata,payload" \
  --output processed.csv
```

### Scenario B: Native Nested JSON

```bash
pii-faker process data.ndjson \
  --extra-pii-columns "patient.patient_name,patient.contacts[*].email" \
  --output processed.ndjson
```

### Scenario C: JSON Path Notation

```
patient.patient_name
patient.contacts[*].email
data.members[*].additional_member_code
```

Bare names like `email` match at any depth by default. Use `--strict-paths` for top-level only.

## Configuration

### Default PII Columns

Built-in patterns include: `name`, `first_name`, `full_name`, `patient_name`, `member_name`, `nama`, `phone`, `mobile`, `email`, `address`, `alamat`, `card_number`, `member_code`, `nik`, `npwp`, `bpjs`, `tanggal_lahir`, etc.

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

Custom columns are stored in the user config file (`~/Library/Application
Support/pii-faker/config.yaml` on macOS, `~/.config/pii-faker/config.yaml` on
Linux; legacy `~/.pii_faker/config.yaml` still works).

### Match Strategies

- **exact**: Column name must match exactly
- **normalized** (default): Case-insensitive, ignores spaces/underscores/hyphens
- **fuzzy**: Token-based matching (no more `username`/`filename` false positives)

### Value-based detection

Even when column names reveal nothing (`col_7`, `notes`), values are sampled
(up to 500 rows) and matched against email, phone, credit-card (Luhn-checked),
IBAN, IPv4, URL-with-credentials, NIK (plausibility-checked), NPWP and BPJS
shapes. Every target gets a confidence score (`0.5·name + 0.5·value`), shown
in `--dry-run` and the web preview. Processed output is automatically
re-scanned (leak check) — any surviving original value or unprocessed
PII-shaped column is reported as a warning.

## File Formats

| Format | Read | Write | Notes |
|--------|------|-------|-------|
| CSV/TSV | ✓ | ✓ | Eager reads today (`read_lazy` exists but the pipeline reads eagerly) |
| Excel | ✓ | ✓ | Uses calamine (fastexcel) for reads; multi-sheet writes go to sibling files (`output_Sheet1.xlsx`) |
| JSON | ✓ | ✓ | Schema inference configurable |
| NDJSON/JSONL | ✓ | ✓ | Eager reads today |
| Parquet | ✓ | ✓ | Best for large datasets |

## Security Model

### Where Mappings Live

- Mapping files are stored locally and never sent to the LLM
- Save them wherever you like via `--mapping-out`; there is no automatic default directory
- JSON (`.json`, small mappings) and SQLite (`.db`/`.sqlite`, large mappings) formats — chosen by `--mapping-out` extension
- Mappings are v2: native value types, per-sheet scope, session ID, and tool version. Version 1 files still load.
- On Unix mapping files are created readable by the owner only (mode `0600`), including when you overwrite an existing, more open file, and encrypted ones too. `save` also refuses a mapping that sends two originals to one replacement in `fake`/`token` modes, since it could not be restored exactly

> Note: SQLite changes only the file format, not memory use — the full
> mapping is still loaded into memory to save and to restore.

### What's in Mappings

- Original → replacement value pairs (native types preserved: int, date, …)
- Source column/path and sheet scope for each mapping
- Processing mode and configuration used
- Timestamp, tool version, and session ID

### Cleanup Guidance

```bash
# Delete mappings older than 7 days
pii-faker cleanup --older-than 7d

# Delete mappings older than 24 hours
pii-faker cleanup --older-than 24h

# Specify custom directory
pii-faker cleanup --older-than 7d --directory /path/to/mappings
```

**Important**: Mapping files contain real PII. Treat them as sensitive data.

### Web UI security

- **Token required.** Every route except the sign-in page needs the access token (a random 256-bit value unless you set your own). A browser exchanges it once for a session cookie (`HttpOnly`, `SameSite=Strict`, 8 hours) and the token is not kept in the browser. Responses are marked `no-store`. The desktop app signs its window in with its per-launch token the same way, and still sends its `X-PII-Session` header
- **One shared token, no accounts.** Everyone holding it can see every job. It suits a single user or a small trusted team, not a multi-tenant service
- **No rate limiting.** That is why a token you supply must be at least 16 characters
- **Plain HTTP.** The server binds to `127.0.0.1` by default. If you listen on a network address, the token and your data travel unencrypted unless you put a TLS-terminating proxy in front; the server warns when you do
- **Files are temporary.** Uploads, outputs and mappings live in a private temp directory. Jobs are deleted after `--retention-minutes` (default 60) without activity, on request with `DELETE /api/jobs/<job id>`, after you download a result, and when the server stops. The desktop app keeps its jobs until it closes
- **Local-path processing is desktop-only.** `/api/detect-path` and `/api/process-path` read and write whatever path the client names, which is right for the single-user desktop app and wrong for a server others can reach, so `pii-faker serve` answers 403 for them
- Jobs and sessions are held in memory by a single process, so restarting the server signs everyone out and drops every job

## Performance

### Unique-Values-First Approach

The toolkit builds mappings only for unique values, then applies them vectorized:

- 5M rows with 40k distinct names → ~125x fewer Faker calls
- Automatic consistency (same original → same fake everywhere)
- Polars `replace()` for vectorized application

### Benchmarks

```bash
python -m pii_faker.tests.benchmark
```

Expected throughput: 100k+ rows/second for typical datasets.

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=pii_faker

# Run specific test file
pytest pii_faker/tests/test_core.py
```

`pip install -e ".[dev]"` brings in everything the suite needs (Hypothesis, `pytest-asyncio` for the proxy tests). The MCP tests need the `mcp` extra and are skipped without it.

| File | Covers |
|---|---|
| `test_core.py`, `test_roundtrip.py` | Unit tests and Hypothesis property tests for the pipeline (round trips of flat, nested, JSON, date and int data) |
| `test_detection.py`, `test_ner.py` | Value-shape detection, confidence, leak check, locale; optional NER |
| `test_phase3.py`, `test_phase4.py`, `test_proxy.py` | Encryption, audit, presets, expiry; LLM round-trip workflow; local proxy |
| `test_privacy.py` | Fakes never contain the original, identifiers, list-of-struct fields are applied, JSON columns keep their structure, the leak check sees inside lists, private mapping files, ambiguous mappings |
| `test_web.py` | Web UI sign-in and token checks, job expiry and cleanup, desktop interplay, desktop-only local paths, detect → process → restore |
| `test_mcp.py` | MCP gateway: what is exposed, that no tool returns a real value (canary test), error handling, restore tool, HTTP auth and host checks |

A round-trip test passing does not show the output is clean: an unprocessed column also "restores exactly". `test_privacy.py` checks the output itself.

## Limitations & Next Steps

### Current Limitations

- Excel is inherently the slowest format; prefer CSV/NDJSON for large exports
- Fuzzy matching may produce false positives on unrelated columns
- Very large mappings (>100k entries) load fully into memory on save and restore, including SQLite mappings
- `redact --redact-style full/partial` is not exactly reversible (many-to-one placeholders); use `fake`, `token`, or `typed-token` when lossless restore matters
- Non-string columns (int, date, …) are faked with native-type fakes; columns with unsupported dtypes are skipped with a warning
- Nested detection looks one list level deep: PII inside a list of lists is not detected, so `process` leaves it as it is (the MCP gateway drops such columns instead)
- Web UI: one shared access token (no per-user accounts), plain HTTP, jobs held in memory by a single process

### Next Steps

- Encryption-at-rest for mapping files
- Additional output formats
- Multi-user deployment considerations
- Streaming support for very large files
- Integration with data pipelines

## License

MIT License

## Contributing

Contributions welcome! Open an issue or pull request on GitHub.
