# PII Faker

A Python toolkit for detecting, faking, or redacting Personally Identifiable Information (PII) in tabular and JSON datasets before sending them to Large Language Models (LLMs).

## Features

- **PII Detection**: Automatically detects PII columns using configurable matching strategies (exact, normalized, fuzzy)
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
```

## Quick Start

### CLI Usage

```bash
# Fake PII in a CSV file
pii-faker process data.csv --output processed.csv --mapping-out mapping.json

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
3. Select columns to process
4. Choose fake or redact mode
5. Download processed files and mapping (server temp files are deleted after download)

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

Built-in patterns include: `name`, `patient_name`, `member_name`, `phone`, `email`, `address`, `card_number`, `member_code`, etc.

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

Custom columns are stored in `~/.pii_faker/config.yaml`.

### Match Strategies

- **exact**: Column name must match exactly
- **normalized** (default): Case-insensitive, ignores spaces/underscores/hyphens
- **fuzzy**: Substring and similarity matching

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
