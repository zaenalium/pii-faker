"""
PII Faker CLI

Command-line interface for processing PII in datasets.
Supports: process, restore, columns, cleanup, serve commands.
"""

import sys
from pathlib import Path
from typing import Optional, List

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle, MatchStrategy
from pii_faker.core.processor import PIIProcessor
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.restorer import PIIRestorer
from pii_faker.core.file_io import FileHandler

app = typer.Typer(
    name="pii-faker",
    help="PII Fake/Redact Toolkit - Detect, fake, or redact PII in datasets for LLM-safe data sharing.",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool):
    if value:
        console.print("[bold blue]PII Faker[/bold blue] v0.1.0")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True,
        help="Show version and exit."
    ),
):
    """
    PII Faker - Detect, fake, or redact PII in datasets for LLM-safe data sharing.
    """
    pass


def _saved_pii_columns() -> List[str]:
    """Custom columns saved with `pii-faker columns add` in ~/.pii_faker/config.yaml."""
    import yaml

    config_file = Path.home() / ".pii_faker" / "config.yaml"
    if not config_file.exists():
        return []
    with open(config_file, 'r') as f:
        data = yaml.safe_load(f) or {}
    return [str(c) for c in data.get('extra_pii_columns', [])]


@app.command()
def process(
    input_file: str = typer.Argument(..., help="Input file path (CSV, Excel, JSON, NDJSON, Parquet)"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    mapping_out: Optional[str] = typer.Option(None, "--mapping-out", "-m", help="Mapping file path"),
    mode: PIIMode = typer.Option(PIIMode.FAKE, "--mode", help="Processing mode: fake or redact"),
    redact_style: RedactionStyle = typer.Option(
        RedactionStyle.FULL, "--redact-style", help="Redaction style: full, partial, or token"
    ),
    match_strategy: MatchStrategy = typer.Option(
        MatchStrategy.NORMALIZED, "--match-strategy", help="Column matching: exact, normalized, or fuzzy"
    ),
    seed: Optional[int] = typer.Option(None, "--seed", "-s", help="Random seed for reproducibility"),
    extra_pii_columns: Optional[str] = typer.Option(
        None, "--extra-pii-columns", "-e", help="Comma-separated extra PII columns/paths"
    ),
    override_defaults: bool = typer.Option(
        False, "--override-defaults", help="Override default PII columns"
    ),
    json_columns: Optional[str] = typer.Option(
        None, "--json-columns", "-j", help="Columns containing JSON strings"
    ),
    strict_paths: bool = typer.Option(
        False, "--strict-paths", help="Only match top-level fields in nested structures"
    ),
    sheets: Optional[str] = typer.Option(None, "--sheets", help="Excel sheets to process"),
    all_sheets: bool = typer.Option(False, "--all-sheets", help="Process all Excel sheets"),
    infer_schema_length: Optional[int] = typer.Option(
        100, "--infer-schema-length", help="Rows for JSON schema inference (0 for full scan)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be processed without doing it"),
):
    """Process a file to fake or redact PII."""

    # Parse extra columns
    # Columns saved with `pii-faker columns add` always apply, alongside --extra-pii-columns.
    extra_cols = _saved_pii_columns()
    if extra_pii_columns:
        extra_cols += [c.strip() for c in extra_pii_columns.split(',') if c.strip() not in extra_cols]

    json_cols = []
    if json_columns:
        json_cols = [c.strip() for c in json_columns.split(',')]

    # Create config
    config = PIIConfig(
        mode=mode,
        redact_style=redact_style,
        match_strategy=match_strategy,
        seed=seed,
        extra_pii_columns=extra_cols,
        override_defaults=override_defaults,
        json_columns=json_cols,
        strict_paths=strict_paths,
        infer_schema_length=infer_schema_length,
        sheets=sheets,
        all_sheets=all_sheets,
    )

    try:
        # Read input
        file_handler = FileHandler(config)
        console.print(f"[bold]Reading[/bold] {input_file}...")

        data = file_handler.read(input_file, sheets=sheets, all_sheets=all_sheets)

        # Handle multiple sheets
        if isinstance(data, dict):
            sheets_to_process = data
        else:
            sheets_to_process = {"default": data}

        # Detect PII in flat, nested and JSON-string columns
        processor = PIIProcessor(config)
        all_pii_columns = {}

        for sheet_name, df in sheets_to_process.items():
            pii_cols = processor.detect(df)
            if pii_cols:
                all_pii_columns[sheet_name] = pii_cols

        # Dry run - just report findings
        if dry_run:
            _print_dry_run(processor, all_pii_columns, sheets_to_process)
            raise typer.Exit()

        if not all_pii_columns:
            console.print("[yellow]Warning:[/yellow] No PII columns detected.")
            if not typer.confirm("Continue without processing?"):
                raise typer.Abort()

        # Process each sheet
        processed_sheets = {}

        for sheet_name, df in sheets_to_process.items():
            pii_cols = all_pii_columns.get(sheet_name, [])
            if not pii_cols:
                processed_sheets[sheet_name] = df
                continue

            console.print(f"[bold]Processing[/bold] sheet '{sheet_name}'...")
            processed_sheets[sheet_name] = processor.process(df, pii_cols)

        if processor.skipped:
            console.print(
                "[yellow]Warning:[/yellow] matched as PII but left unchanged (no text values "
                f"at that name or path): {', '.join(processor.skipped)}"
            )

        # Save the mapping before writing any output, so a failure here (bad
        # path, ambiguous mapping) never leaves processed data we can't restore.
        if mapping_out:
            processor.mapper.save(mapping_out)
            console.print(f"[bold]Mapping saved[/bold] to {mapping_out}")
        elif all_pii_columns and processor.mapper.is_lossless_mode():
            console.print(
                "[yellow]Warning:[/yellow] no --mapping-out given, so the original values "
                "cannot be restored later."
            )

        # Write output
        console.print(f"[bold]Writing[/bold] {output}...")

        if len(processed_sheets) == 1 and "default" in processed_sheets:
            file_handler.write(processed_sheets["default"], output)
        else:
            # For multiple sheets, write each to a sibling file with the
            # sheet name inserted before the extension.
            output_path = Path(output)
            for sheet_name, df in processed_sheets.items():
                sheet_output = str(
                    output_path.with_name(f"{output_path.stem}_{sheet_name}{output_path.suffix}")
                )
                file_handler.write(df, sheet_output)

        console.print("[bold green]Done![/bold green]")

    except (typer.Exit, typer.Abort):
        raise
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)


def _print_dry_run(
    processor: PIIProcessor,
    all_pii_columns: dict,
    sheets: dict,
):
    """Print dry run report."""
    table = Table(title="PII Detection Report (Dry Run)")
    table.add_column("Sheet", style="cyan")
    table.add_column("Column", style="green")
    table.add_column("Category", style="yellow")
    table.add_column("Pattern Match", style="blue")
    table.add_column("Unique Values", justify="right")

    total_unique = 0
    for sheet_name, pii_cols in all_pii_columns.items():
        df = sheets[sheet_name]
        for col_info in pii_cols:
            unique_count = processor.unique_count(df, col_info)
            total_unique += unique_count
            table.add_row(
                sheet_name,
                col_info['column'],
                col_info['category'],
                col_info['pattern'],
                str(unique_count),
            )

    console.print(table)

    console.print(f"\n[bold]Total unique PII values to process:[/bold] {total_unique}")


@app.command()
def restore(
    input_file: str = typer.Argument(..., help="Input file with faked/redacted data"),
    mapping: str = typer.Option(..., "--mapping", "-m", help="Mapping file path"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    is_text: bool = typer.Option(False, "--text", "-t", help="Input is free-form text"),
):
    """Restore original values from faked/redacted data."""
    try:
        console.print(f"[bold]Loading mapping[/bold] from {mapping}...")
        mapper = PIIMapper.load(mapping)

        restorer = PIIRestorer(mapper)

        if is_text:
            # Handle free-form text
            console.print(f"[bold]Reading[/bold] {input_file}...")
            with open(input_file, 'r') as f:
                text = f.read()

            console.print("[bold]Restoring[/bold] values...")
            restored_text = restorer.restore_text(text)

            console.print(f"[bold]Writing[/bold] {output}...")
            with open(output, 'w') as f:
                f.write(restored_text)
        else:
            # Handle tabular/JSON data
            file_handler = FileHandler(mapper.config)
            console.print(f"[bold]Reading[/bold] {input_file}...")

            df = file_handler.read(input_file)

            if isinstance(df, dict):
                # Multiple sheets
                output_path = Path(output)
                for sheet_name, sheet_df in df.items():
                    sheet_output = str(
                        output_path.with_name(f"{output_path.stem}_{sheet_name}{output_path.suffix}")
                    )
                    restored_df = restorer.restore_dataframe(sheet_df)
                    file_handler.write(restored_df, sheet_output)
            else:
                console.print("[bold]Restoring[/bold] values...")
                restored_df = restorer.restore_dataframe(df)
                console.print(f"[bold]Writing[/bold] {output}...")
                file_handler.write(restored_df, output)

        console.print("[bold green]Done![/bold green]")

    except (typer.Exit, typer.Abort):
        raise
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)


@app.command("columns")
def columns_command(
    action: str = typer.Argument(..., help="Action: add, list, or remove"),
    column: Optional[str] = typer.Argument(None, help="Column name or path"),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c", help="Config file path"
    ),
):
    """Manage custom PII columns."""
    import yaml

    # Default config path
    if config_path is None:
        config_dir = Path.home() / ".pii_faker"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = str(config_dir / "config.yaml")

    # Load existing config
    config_data = {}
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file, 'r') as f:
            config_data = yaml.safe_load(f) or {}

    extra_columns = config_data.get('extra_pii_columns', [])

    if action == "add":
        if column is None:
            console.print("[red]Error:[/red] Column name required for 'add' action.")
            raise typer.Exit(code=1)

        if column not in extra_columns:
            extra_columns.append(column)
            config_data['extra_pii_columns'] = extra_columns
            with open(config_path, 'w') as f:
                yaml.dump(config_data, f, default_flow_style=False)
            console.print(f"[green]Added[/green] '{column}' to PII columns.")
        else:
            console.print(f"[yellow]Column '{column}' already exists.[/yellow]")

    elif action == "list":
        console.print("[bold]Custom PII Columns:[/bold]")
        if extra_columns:
            for col in extra_columns:
                console.print(f"  - {col}")
        else:
            console.print("  (none)")

    elif action == "remove":
        if column is None:
            console.print("[red]Error:[/red] Column name required for 'remove' action.")
            raise typer.Exit(code=1)

        if column in extra_columns:
            extra_columns.remove(column)
            config_data['extra_pii_columns'] = extra_columns
            with open(config_path, 'w') as f:
                yaml.dump(config_data, f, default_flow_style=False)
            console.print(f"[green]Removed[/green] '{column}' from PII columns.")
        else:
            console.print(f"[yellow]Column '{column}' not found.[/yellow]")

    else:
        console.print(f"[red]Error:[/red] Unknown action '{action}'. Use: add, list, remove")
        raise typer.Exit(code=1)


@app.command()
def cleanup(
    older_than: str = typer.Option("7d", "--older-than", help="Delete mappings older than (e.g., 7d, 24h)"),
    directory: Optional[str] = typer.Option(
        None, "--directory", "-d", help="Mappings directory (default: ~/.pii_faker/mappings)"
    ),
):
    """Delete old mapping files (mapping*.json, mapping*.db, mapping*.sqlite)."""
    from datetime import timedelta

    if directory is None:
        directory = str(Path.home() / ".pii_faker" / "mappings")

    # Parse time delta (supports days and hours, incl. fractional)
    try:
        if older_than.endswith('d'):
            older_than_days = float(older_than[:-1])
        elif older_than.endswith('h'):
            older_than_days = float(older_than[:-1]) / 24
        else:
            raise ValueError("missing suffix")
    except ValueError:
        console.print("[red]Error:[/red] Invalid format. Use '7d' for days or '24h' for hours.")
        raise typer.Exit(code=1)

    config = PIIConfig()
    mapper = PIIMapper(config)
    deleted = mapper.cleanup_old_mappings(directory, older_than_days)

    console.print(f"[green]Deleted {len(deleted)} mapping file(s) older than {older_than}[/green]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on"),
    token: Optional[str] = typer.Option(
        None, "--token", envvar="PII_FAKER_TOKEN",
        help="Access token, at least 16 characters (prefer the PII_FAKER_TOKEN environment "
             "variable: command lines are visible to other users). A random one is generated if omitted.",
    ),
    retention_minutes: float = typer.Option(
        60.0, "--retention-minutes", min=1.0,
        help="Delete a job's uploaded file, outputs and mapping after this many minutes without activity",
    ),
):
    """Start the web UI (sign-in with an access token required)."""
    from pii_faker.web.app import run_server

    try:
        # soft_wrap: never break the sign-in link across lines
        run_server(
            host, port, token, retention_minutes,
            announce=lambda message: console.print(message, soft_wrap=True, markup=False, highlight=False),
        )
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)


def _print_mcp_report(err: Console, reports: list) -> None:
    """What each dataset exposes to the model. Column names only, never values."""
    for report in reports:
        table = Table(title=f"Dataset '{report.name}' ({report.rows} rows): what the model can see")
        table.add_column("Column / field", style="green")
        table.add_column("Exposed as", style="yellow")
        for key in report.faked:
            if key not in report.blocked:
                table.add_row(key, "faked")
        for key in report.kept:
            table.add_row(key, "[red]original values[/red] (kept)")
        for key in report.pruned:
            table.add_row(key, "dropped (inside an exposed column)")
        for key in report.dropped:
            note = "dropped: looks like PII but holds no text" if key in report.blocked else "dropped"
            table.add_row(key, note)
        err.print(table)


@app.command("mcp")
def mcp_command(
    dataset: List[str] = typer.Option(
        ..., "--dataset", "-d", help="name=path/to/file. Repeat for several. The model only ever sees the name."
    ),
    keep: Optional[str] = typer.Option(
        None, "--keep",
        help="Comma-separated columns or nested paths to expose with their ORIGINAL values. "
             "Everything not faked and not listed here is dropped.",
    ),
    keep_unmatched: bool = typer.Option(
        False, "--keep-unmatched",
        help="Expose every column the detector did not recognise, as-is. NOT recommended for an "
             "untrusted provider: the detector only reads column names.",
    ),
    mode: PIIMode = typer.Option(PIIMode.FAKE, "--mode", help="fake (default) or redact"),
    redact_style: RedactionStyle = typer.Option(
        RedactionStyle.FULL, "--redact-style", help="full or token (partial is refused: it leaks characters)"
    ),
    match_strategy: MatchStrategy = typer.Option(MatchStrategy.NORMALIZED, "--match-strategy"),
    seed: Optional[int] = typer.Option(None, "--seed", "-s", help="Random seed for reproducible fakes"),
    extra_pii_columns: Optional[str] = typer.Option(
        None, "--extra-pii-columns", "-e", help="Comma-separated extra PII columns/paths to fake"
    ),
    override_defaults: bool = typer.Option(False, "--override-defaults", help="Ignore the built-in column names"),
    json_columns: Optional[str] = typer.Option(None, "--json-columns", "-j", help="Columns holding JSON text"),
    strict_paths: bool = typer.Option(False, "--strict-paths", help="Bare names match top-level fields only"),
    infer_schema_length: Optional[int] = typer.Option(100, "--infer-schema-length", help="0 reads every row"),
    mapping_out: Optional[str] = typer.Option(
        None, "--mapping-out", "-m",
        help="Save the real<->fake mapping here (private file, never exposed to the model) so you can restore later",
    ),
    restore_dir: Optional[str] = typer.Option(
        None, "--restore-dir",
        help="Enable a tool that restores real values into text and saves it in this folder. "
             "The model only gets a count back. Never expose this folder to the model.",
    ),
    max_rows: int = typer.Option(100, "--max-rows", min=1, help="Most rows any tool call returns"),
    transport: str = typer.Option("stdio", "--transport", help="stdio (local) or http (remote / tunnel)"),
    host: str = typer.Option("127.0.0.1", "--host", help="http only: address to bind"),
    port: int = typer.Option(8765, "--port", "-p", help="http only: port"),
    token: Optional[str] = typer.Option(
        None, "--token", envvar="PII_FAKER_MCP_TOKEN",
        help="http only: access token, at least 16 characters (prefer the PII_FAKER_MCP_TOKEN environment "
             "variable). Generated and printed if omitted.",
    ),
    allowed_host: List[str] = typer.Option(
        [], "--allowed-host",
        help="http only: a hostname the server may be reached by, such as your tunnel's. Repeatable.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what the model would see, then exit"),
):
    """Serve datasets to an LLM over MCP with all PII faked (safe for untrusted providers)."""
    import importlib.util

    err = Console(stderr=True)      # stdout carries the protocol in stdio mode

    if importlib.util.find_spec("mcp") is None:
        err.print("[bold red]Error:[/bold red] the MCP SDK is not installed. Run: pip install 'pii-faker[mcp]'")
        raise typer.Exit(code=1)
    if transport not in ("stdio", "http"):
        err.print("[bold red]Error:[/bold red] --transport must be stdio or http.")
        raise typer.Exit(code=1)

    from pii_faker.mcp_server import Gateway, parse_dataset_specs, run_gateway

    try:
        sources = parse_dataset_specs(dataset)

        extra_cols = _saved_pii_columns()
        if extra_pii_columns:
            extra_cols += [c.strip() for c in extra_pii_columns.split(',') if c.strip() not in extra_cols]

        config = PIIConfig(
            mode=mode,
            redact_style=redact_style,
            match_strategy=match_strategy,
            seed=seed,
            extra_pii_columns=extra_cols,
            override_defaults=override_defaults,
            json_columns=[c.strip() for c in json_columns.split(',')] if json_columns else [],
            strict_paths=strict_paths,
            infer_schema_length=infer_schema_length,
        )
        gateway = Gateway(
            config,
            keep=keep.split(',') if keep else [],
            keep_unmatched=keep_unmatched,
            max_rows=max_rows,
            restore_dir=Path(restore_dir) if restore_dir else None,
        )
        for name, path in sources.items():
            err.print(f"[bold]Loading[/bold] {name} from {path}...")
            gateway.add_dataset(name, path)

        _print_mcp_report(err, gateway.reports())
        if gateway.processor.skipped:
            err.print(
                "[yellow]Warning:[/yellow] matched as PII but holds no text, so it is NOT shown to the model: "
                + ", ".join(gateway.processor.skipped)
            )
        if keep_unmatched:
            err.print("[yellow]Warning:[/yellow] --keep-unmatched exposes every unrecognised column as-is.")

        if dry_run:
            err.print("Dry run: nothing is being served.")
            raise typer.Exit()

        if mapping_out:
            gateway.save_mapping(mapping_out)
            err.print(f"[bold]Mapping saved[/bold] to {mapping_out} (not visible to the model)")
        elif not gateway.can_restore and gateway.processor.mapper.is_lossless_mode():
            err.print("[yellow]Warning:[/yellow] no --mapping-out or --restore-dir: the real values can't be "
                      "recovered once this server stops.")

        err.print(f"[bold green]Serving[/bold green] {len(sources)} dataset(s) over {transport}. "
                  "Stop with Ctrl+C.")
        run_gateway(
            gateway, transport, host, port, token, allowed_host,
            announce=lambda message: err.print(message, soft_wrap=True, markup=False, highlight=False),
        )
    except (typer.Exit, typer.Abort):
        raise
    except ValueError as e:
        err.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
