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
- **CLI & Web UI**: Command-line interface and web-based interface
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

Open http://localhost:8000 in your browser to:
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
```

> Note: the web UI runs on localhost only and is designed for a single
> trusted user. Uploaded originals and plaintext mappings live in a temp
> directory until you download the results, then are deleted.

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

## Limitations & Next Steps

### Current Limitations

- Excel is inherently the slowest format; prefer CSV/NDJSON for large exports
- Fuzzy matching may produce false positives on unrelated columns
- Very large mappings (>100k entries) load fully into memory on save and restore, including SQLite mappings
- `redact --redact-style full/partial` is not exactly reversible (many-to-one placeholders); use `fake`, `token`, or `typed-token` when lossless restore matters
- Non-string columns (int, date, …) are faked with native-type fakes; columns with unsupported dtypes are skipped with a warning

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
