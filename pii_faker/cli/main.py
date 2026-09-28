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
from pii_faker.core.detector import PIIColumnDetector
from pii_faker.core.generators import PIIGenerator
from pii_faker.core.redactor import PIIRedactor
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
    extra_cols = []
    if extra_pii_columns:
        extra_cols = [c.strip() for c in extra_pii_columns.split(',')]

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
        infer_schema_length=infer_schema_length if infer_schema_length != 0 else None,
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

        # Detect PII columns
        detector = PIIColumnDetector(config)
        all_pii_columns = {}

        for sheet_name, df in sheets_to_process.items():
            pii_cols = detector.detect(df)
            if pii_cols:
                all_pii_columns[sheet_name] = pii_cols

        # Dry run - just report findings
        if dry_run:
            _print_dry_run(all_pii_columns, sheets_to_process)
            raise typer.Exit()

        if not all_pii_columns:
            console.print("[yellow]Warning:[/yellow] No PII columns detected.")
            if not typer.confirm("Continue without processing?"):
                raise typer.Abort()

        # Process each sheet
        mapper = PIIMapper(config)
        processed_sheets = {}

        for sheet_name, df in sheets_to_process.items():
            pii_cols = all_pii_columns.get(sheet_name, [])
            if not pii_cols:
                processed_sheets[sheet_name] = df
                continue

            console.print(f"[bold]Processing[/bold] sheet '{sheet_name}'...")

            if config.mode == PIIMode.FAKE:
                generator = PIIGenerator(config)
                for col_info in pii_cols:
                    df, col_mapping = generator.process_column(df, col_info)
                    mapper.add_column_mapping(
                        col_info['column'], col_info['category'], col_mapping
                    )
            else:
                redactor = PIIRedactor(config)
                for col_info in pii_cols:
                    df, col_mapping = redactor.process_column(df, col_info)
                    mapper.add_column_mapping(
                        col_info['column'], col_info['category'], col_mapping
                    )

            processed_sheets[sheet_name] = df

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

        # Save mapping
        if mapping_out:
            mapper.save(mapping_out)
            console.print(f"[bold]Mapping saved[/bold] to {mapping_out}")

        console.print("[bold green]Done![/bold green]")

    except (typer.Exit, typer.Abort):
        raise
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)


def _print_dry_run(
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

    for sheet_name, pii_cols in all_pii_columns.items():
        df = sheets[sheet_name]
        for col_info in pii_cols:
            col_name = col_info['column']
            unique_count = df[col_name].n_unique()
            table.add_row(
                sheet_name,
                col_name,
                col_info['category'],
                col_info['pattern'],
                str(unique_count),
            )

    console.print(table)

    total_unique = 0
    for sheet_name, pii_cols in all_pii_columns.items():
        df = sheets[sheet_name]
        for col_info in pii_cols:
            total_unique += df[col_info['column']].n_unique()

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
    directory: str = typer.Option(
        str(Path.home() / ".pii_faker" / "mappings"),
        "--directory", "-d", help="Mappings directory"
    ),
):
    """Clean up old mapping files."""
    from datetime import timedelta

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
    mapper.cleanup_old_mappings(directory, older_than_days)

    console.print(f"[green]Cleaned up mappings older than {older_than}[/green]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on"),
):
    """Start the web UI server."""
    import uvicorn
    from pii_faker.web.app import app as web_app

    console.print(f"[bold]Starting[/bold] PII Faker web UI on {host}:{port}...")
    uvicorn.run(web_app, host=host, port=port)


if __name__ == "__main__":
    app()
