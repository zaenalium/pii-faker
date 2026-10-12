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
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.restorer import PIIRestorer
from pii_faker.core.file_io import FileHandler
from pii_faker.core.pipeline import (
    detect_targets,
    process_sheets,
    restore_sheets,
)

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
    try:
        from pii_faker.core.config import auto_expire_mappings
        auto_expire_mappings()
    except Exception:
        pass
    pass


@app.command()
def process(
    input_file: str = typer.Argument(..., help="Input file path (CSV, Excel, JSON, NDJSON, Parquet)"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    mapping_out: Optional[str] = typer.Option(None, "--mapping-out", "-m", help="Mapping file path"),
    mode: PIIMode = typer.Option(PIIMode.FAKE, "--mode", help="Processing mode: fake or redact (redact is LLM-safe)"),
    redact_style: RedactionStyle = typer.Option(
        RedactionStyle.TYPED_TOKEN, "--redact-style", help="Redaction style: typed-token (LLM default), full, partial, or token"
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
    locale: Optional[str] = typer.Option(
        None, "--locale", help="Faker locale for fake data (e.g., id_ID, en_US)"
    ),
    preset: Optional[str] = typer.Option(
        None, "--preset", help="Compliance preset (e.g., id for Indonesia/UU PDP)"
    ),
    encrypt: bool = typer.Option(
        False, "--encrypt", help="Encrypt the mapping file (AES-GCM, keychain/passphrase)"
    ),
    passphrase: Optional[str] = typer.Option(
        None, "--passphrase", help="Passphrase for mapping encryption (or set PII_FAKER_PASSPHRASE)"
    ),
    audit_out: Optional[str] = typer.Option(
        None, "--audit-out", help="Write a PII-free audit report (JSON) to this path"
    ),
    sheets: Optional[str] = typer.Option(None, "--sheets", help="Excel sheets to process"),
    all_sheets: bool = typer.Option(False, "--all-sheets", help="Process all Excel sheets"),
    infer_schema_length: Optional[int] = typer.Option(
        100, "--infer-schema-length", help="Rows for JSON schema inference (0 for full scan)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be processed without doing it"),
    shift_days: Optional[int] = typer.Option(
        None, "--shift-days", help="Date-shift mode: fixed day offset (implies --mode shift)"
    ),
    perturb_pct: Optional[float] = typer.Option(
        None, "--perturb-pct", help="Perturb mode: ±percent numeric noise (implies --mode perturb)"
    ),
    project_mapping: Optional[str] = typer.Option(
        None, "--project-mapping", help="Project mode (4.5): reuse/add to this shared mapping file"
    ),
):
    """Process a file to fake or redact PII."""

    # Mode implications (4.4)
    if shift_days is not None and mode == PIIMode.FAKE:
        mode = PIIMode.SHIFT
    if perturb_pct is not None and mode == PIIMode.FAKE:
        mode = PIIMode.PERTURB

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
        locale=locale,
        infer_schema_length=infer_schema_length if infer_schema_length != 0 else None,
        sheets=sheets,
        all_sheets=all_sheets,
        shift_days=shift_days,
        perturb_pct=perturb_pct,
    )
    if preset:
        from pii_faker.core.presets import apply_preset

        config = apply_preset(config, preset)

    try:
        # Read input
        file_handler = FileHandler(config)
        console.print(f"[bold]Reading[/bold] {input_file}...")

        data = file_handler.read(input_file, sheets=sheets, all_sheets=all_sheets)

        # Handle multiple sheets (single pipeline: shared RunState + scoped mapper)
        if isinstance(data, dict):
            sheets_to_process = data
        else:
            sheets_to_process = {"default": data}

        # Detect PII targets via the pipeline plan (flat + nested + JSON-string)
        all_pii_columns = {}
        all_plans = {}

        for sheet_name, df in sheets_to_process.items():
            plan = detect_targets(df, config)
            all_plans[sheet_name] = plan
            if plan.targets:
                all_pii_columns[sheet_name] = [
                    {
                        'column': t.column,
                        'path': t.path,
                        'category': t.category,
                        'pattern': t.pattern,
                        'kind': t.kind,
                        'confidence': t.confidence,
                        'name_match': t.name_match,
                        'value_detector': t.value_detector,
                    }
                    for t in plan.targets
                ]

        # Dry run - just report findings
        if dry_run:
            _print_dry_run(all_pii_columns, sheets_to_process, config)
            raise typer.Exit()

        if not all_pii_columns:
            console.print("[yellow]Warning:[/yellow] No PII columns detected.")
            if not typer.confirm("Continue without processing?"):
                raise typer.Abort()

        # Process all sheets through the single pipeline
        for sheet_name, df in sheets_to_process.items():
            plan = all_plans[sheet_name]
            if plan.targets:
                kinds = sorted({t.kind for t in plan.targets})
                console.print(
                    f"[bold]Processing[/bold] sheet '{sheet_name}' "
                    f"({len(plan.targets)} targets: {', '.join(kinds)})..."
                )
        processed_sheets, mapper, run_state = process_sheets(
            sheets_to_process, config
        )
        for warning in dict.fromkeys(run_state.warnings):
            console.print(f"[yellow]Warning:[/yellow] {warning}")

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
            if encrypt:
                mapper.save(mapping_out, encrypt=True, passphrase=passphrase)
            else:
                mapper.save(mapping_out)
            console.print(f"[bold]Mapping saved[/bold] to {mapping_out}")
            try:
                audit_json = mapping_out.replace(".json", "_audit.json").replace(".db", "_audit.json")
                if Path(audit_json).exists():
                    console.print(f"[dim]Audit: {audit_json}[/dim]")
            except Exception:
                pass
        if audit_out:
            from pii_faker.core.audit import build_audit
            import datetime as _dt

            # Reuse the audit already built on run_state if present; else build one from the sheets.
            audit = getattr(run_state, "audit", None)
            if audit is None:
                all_targets: list = []
                counts: dict = {}
                for sheet_name, frame in sheets_to_process.items():
                    from pii_faker.core.pipeline import plan_dataframe

                    plan = plan_dataframe(frame, config)
                    all_targets.extend(plan.targets)
                    for t in plan.targets:
                        try:
                            counts[t.path] = int(frame[t.column].n_unique())
                        except Exception:
                            counts[t.path] = 0
                audit = build_audit(
                    run_id=mapper.session_id,
                    started_at=_dt.datetime.now(),
                    finished_at=_dt.datetime.now(),
                    config=config,
                    targets=all_targets,
                    counts=counts,
                    warnings=list(dict.fromkeys(run_state.warnings)),
                    leak_report={"clean": True},
                )
            import json as _json

            Path(audit_out).write_text(_json.dumps(audit.to_dict() if hasattr(audit, "to_dict") else audit, indent=2, ensure_ascii=False))
            console.print(f"[dim]Audit: {audit_out}[/dim]")

        console.print("[bold green]Done![/bold green]")

    except (typer.Exit, typer.Abort):
        raise
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)


def _print_dry_run(
    all_pii_columns: dict,
    sheets: dict,
    config=None,
):
    """Print dry run report."""
    table = Table(title="PII Detection Report (Dry Run)")
    table.add_column("Sheet", style="cyan")
    table.add_column("Target", style="green")
    table.add_column("Category", style="yellow")
    table.add_column("Pattern Match", style="blue")
    table.add_column("Kind", style="magenta")
    table.add_column("Confidence", justify="right")
    table.add_column("Signal", style="dim")
    table.add_column("Unique Values", justify="right")

    for sheet_name, pii_cols in all_pii_columns.items():
        df = sheets[sheet_name]
        for col_info in pii_cols:
            col_name = col_info['column']
            try:
                unique_count = df[col_name].n_unique()
            except Exception:
                unique_count = 0
            if col_info.get('name_match', True):
                signal = "name"
            else:
                signal = f"value:{col_info.get('value_detector', '?')}"
            table.add_row(
                sheet_name,
                col_info.get('path', col_name),
                col_info['category'],
                col_info['pattern'],
                col_info.get('kind', 'flat'),
                f"{col_info.get('confidence', 1.0):.2f}",
                signal,
                str(unique_count),
            )

    console.print(table)

    total_unique = 0
    for sheet_name, pii_cols in all_pii_columns.items():
        df = sheets[sheet_name]
        for col_info in pii_cols:
            try:
                total_unique += df[col_info['column']].n_unique()
            except Exception:
                pass

    console.print(f"\n[bold]Total unique PII values to process:[/bold] {total_unique}")

    # Locale auto-suggestion (2.5)
    from pii_faker.core.value_detectors import scan_frame_values, suggest_locale

    all_columns: list = []
    value_findings: dict = {}
    for sheet_name, df in sheets.items():
        all_columns.extend(df.columns)
        for col, matches in scan_frame_values(df).items():
            value_findings[f"{sheet_name}.{col}"] = matches
    suggestion = suggest_locale(value_findings, all_columns)
    if suggestion and suggestion != (config.locale or "en_US"):
        console.print(
            f"[yellow]Suggestion:[/yellow] Indonesian identifiers detected — "
            f"consider [bold]--locale {suggestion}[/bold] for localised fakes."
        )


@app.command()
def restore(
    input_file: str = typer.Argument(..., help="Input file with faked/redacted data"),
    mapping: str = typer.Option(..., "--mapping", "-m", help="Mapping file path"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    is_text: bool = typer.Option(False, "--text", "-t", help="Input is free-form text"),
    passphrase: Optional[str] = typer.Option(None, "--passphrase", help="Passphrase for encrypted mapping"),
):
    """Restore original values from faked/redacted data."""
    try:
        console.print(f"[bold]Loading mapping[/bold] from {mapping}...")
        mapper = PIIMapper.load(mapping, passphrase=passphrase)

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
            # Handle tabular/JSON data (scope-aware restore)
            file_handler = FileHandler(mapper.config)
            console.print(f"[bold]Reading[/bold] {input_file}...")

            df = file_handler.read(input_file)

            if isinstance(df, dict):
                # Multiple sheets
                output_path = Path(output)
                restored = restore_sheets(df, mapper)
                for sheet_name, restored_df in restored.items():
                    sheet_output = str(
                        output_path.with_name(f"{output_path.stem}_{sheet_name}{output_path.suffix}")
                    )
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

    from pii_faker.core.config import user_config_path

    # Default config path (platformdirs, with legacy ~/.pii_faker fallback)
    if config_path is None:
        resolved = user_config_path()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        config_path = str(resolved)

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


@app.command("presets")
def presets_command(
    action: str = typer.Argument(..., help="Action: list or show"),
    name: Optional[str] = typer.Argument(None, help="Preset name (for show)"),
):
    """List or show compliance presets."""
    from pii_faker.core.presets import list_presets, load_preset

    if action == "list":
        presets = list_presets()
        if not presets:
            console.print("  (none)")
            return
        table = Table(title="Presets")
        table.add_column("Name", style="cyan")
        table.add_column("Locale", style="green")
        table.add_column("Description")
        for p in presets:
            table.add_row(p["name"], p.get("locale", ""), p.get("description", ""))
        console.print(table)
    elif action == "show":
        if not name:
            console.print("[red]Error:[/red] Preset name required.")
            raise typer.Exit(code=1)
        data = load_preset(name)
        rprint(data)
    else:
        console.print(f"[red]Error:[/red] Unknown action {action!r}. Use: list, show")
        raise typer.Exit(code=1)


key_app = typer.Typer(help="Manage the encryption master key in the OS keychain.", no_args_is_help=True)
app.add_typer(key_app, name="key")


@key_app.command("set")
def key_set():
    """Generate and store a new 32-byte master key in the OS keychain."""
    from pii_faker.core.crypto import keyring_set

    keyring_set()
    console.print("[green]Master key stored in keychain (service pii-faker, account master).[/green]")


@key_app.command("show")
def key_show():
    """Show whether a master key exists (never prints the raw key)."""
    from pii_faker.core.crypto import keyring_exists

    if keyring_exists():
        console.print("[green]Master key exists[/green] in keychain (pii-faker/master).")
    else:
        console.print("[yellow]No master key[/yellow] — run: pii-faker key set")


@key_app.command("delete")
def key_delete():
    """Delete the master key from the OS keychain."""
    from pii_faker.core.crypto import keyring_delete

    if keyring_delete():
        console.print("[green]Master key deleted.[/green]")
    else:
        console.print("[yellow]No key to delete, or delete failed.[/yellow]")


@app.command("project")
def project_command(
    action: str = typer.Argument(..., help="Action: init, add, status"),
    name: Optional[str] = typer.Argument(None, help="Project name"),
    mapping: Optional[str] = typer.Option(None, "--mapping", "-m", help="Shared project mapping file"),
):
    """Project mode (4.5): one shared mapping across multiple files.

    init: create an empty project mapping. add: merge a per-file mapping
    into the project mapping (same original → same fake is already enforced
    by cross-scope reuse, so add is a union).
    """
    from pii_faker.core.mapper import PIIMapper

    if action == "init":
        if not name or not mapping:
            console.print("[red]Error:[/red] project init needs a NAME and --mapping path.")
            raise typer.Exit(code=1)
        m = PIIMapper(PIIConfig())
        m.save(mapping)
        console.print(f"[green]Project {name!r} initialised:[/green] {mapping}")
    elif action == "add":
        if not mapping:
            console.print("[red]Error:[/red] project add needs --mapping (project file) plus a FILE mapping.")
            raise typer.Exit(code=1)
        if not name:
            console.print("[red]Error:[/red] project add needs the per-file mapping path as NAME.")
            raise typer.Exit(code=1)
        project = PIIMapper.load(mapping) if Path(mapping).exists() else PIIMapper(PIIConfig())
        incoming = PIIMapper.load(name)
        project.merge(incoming)
        project.save(mapping)
        console.print(f"[green]Merged[/green] {name} into {mapping}")
    elif action == "status":
        if not mapping:
            console.print("[red]Error:[/red] project status needs --mapping.")
            raise typer.Exit(code=1)
        project = PIIMapper.load(mapping)
        entries = len(project.store._entries)
        scopes = project.store.scopes()
        console.print(f"[bold]Entries:[/bold] {entries}  [bold]Scopes:[/bold] {', '.join(scopes) or '(none)'}")
    else:
        console.print(f"[red]Error:[/red] Unknown action {action!r}. Use: init, add, status")
        raise typer.Exit(code=1)


@app.command()
def cleanup(
    older_than: str = typer.Option("7d", "--older-than", help="Delete mappings older than (e.g., 7d, 24h)"),
    directory: Optional[str] = typer.Option(
        None, "--directory", "-d", help="Mappings directory (default: app data dir)"
    ),
):
    """Clean up old mapping files."""
    from datetime import timedelta

    from pii_faker.core.config import mappings_dir

    if directory is None:
        directory = str(mappings_dir())

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
