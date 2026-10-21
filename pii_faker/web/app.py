"""
PII Faker Web Application

FastAPI-based web interface for PII processing.
Provides file upload, detection preview, processing, and restore functionality.

Every route except the sign-in page requires the access token (see auth.py).
Uploads, outputs and mappings live in a private temp directory, are deleted
after a period without activity, and are all removed when the server stops.
"""

import asyncio
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import polars as pl
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle, MatchStrategy
from pii_faker.core.detector import PIIColumnDetector
from pii_faker.core.json_walker import JsonWalker
from pii_faker.core.processor import PIIProcessor
from pii_faker.core.restorer import PIIRestorer
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.file_io import FileHandler
from pii_faker.web.auth import (
    TOKEN_ENV,
    SESSION_COOKIE,
    Authenticator,
    session_cookie_options,
)

DEFAULT_RETENTION_MINUTES = 60

# In-memory job storage. Single process only: sessions and jobs are not shared
# between workers.
jobs: Dict[str, Dict[str, Any]] = {}
_job_root: Optional[Path] = None


def _new_job_dir(prefix: str) -> Path:
    """A private (0700) directory for one job, under a per-server root."""
    global _job_root
    if _job_root is None or not _job_root.exists():
        _job_root = Path(tempfile.mkdtemp(prefix="pii_faker_web_"))
    return Path(tempfile.mkdtemp(prefix=prefix, dir=_job_root))


def _store_job(job_id: str, **fields: Any) -> None:
    jobs[job_id] = {**fields, "touched": time.time()}


def _get_job(job_id: str) -> Dict[str, Any]:
    """Look up a job and count the access as activity (it postpones expiry)."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job["touched"] = time.time()
    return job


def _drop_job(job_id: str) -> None:
    job = jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["temp_dir"], ignore_errors=True)


def purge_expired_jobs(max_age_seconds: float, now: Optional[float] = None) -> int:
    """Delete jobs (and their files) idle for longer than `max_age_seconds`."""
    now = time.time() if now is None else now
    expired = [job_id for job_id, job in jobs.items() if now - job["touched"] > max_age_seconds]
    for job_id in expired:
        _drop_job(job_id)
    return len(expired)


def purge_all_jobs() -> None:
    """Delete every job and the server's temp root."""
    global _job_root
    for job_id in list(jobs):
        _drop_job(job_id)
    if _job_root is not None:
        shutil.rmtree(_job_root, ignore_errors=True)
        _job_root = None


def _safe_filename(filename: Optional[str], default: str) -> str:
    """Strip any directory part from a client-supplied upload name."""
    name = Path((filename or "").replace("\\", "/")).name
    return name if name not in ("", ".", "..") else default


async def _reap_expired_jobs(app: FastAPI) -> None:
    while True:
        retention = app.state.retention_seconds
        await asyncio.sleep(max(1.0, min(60.0, retention / 2)))
        purge_expired_jobs(retention)


@asynccontextmanager
async def lifespan(app: FastAPI):
    reaper = asyncio.create_task(_reap_expired_jobs(app))
    try:
        yield
    finally:
        reaper.cancel()
        purge_all_jobs()


app = FastAPI(
    title="PII Faker",
    description="PII Fake/Redact Toolkit - Web Interface",
    version="0.1.0",
    lifespan=lifespan,
)

# Never open by default: a token is generated unless run_server() is given one.
app.state.auth = Authenticator()
app.state.retention_seconds = DEFAULT_RETENTION_MINUTES * 60

# Setup static files and templates with absolute paths
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Ensure directories exist
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Everything else needs authentication, including routes added later.
PUBLIC_PATHS = {"/", "/login"}

RESPONSE_HEADERS = {
    "Cache-Control": "no-store",          # responses can contain PII
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    authenticated = request.app.state.auth.is_authenticated(request)
    if request.url.path in PUBLIC_PATHS or authenticated:
        response = await call_next(request)
    else:
        response = JSONResponse(
            {"detail": "Not authenticated"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    for name, value in RESPONSE_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def _login_page(error: Optional[str] = None) -> HTMLResponse:
    html = templates.get_template("login.html").render(error=error)
    return HTMLResponse(content=html, status_code=401)


def _sign_in(request: Request) -> RedirectResponse:
    auth = request.app.state.auth
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(value=auth.open_session(), **session_cookie_options(request, auth.session_seconds))
    return response


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, token: Optional[str] = None):
    """Home page with file upload form (or the sign-in page)."""
    auth = request.app.state.auth

    if token is not None:
        # Opened from the link printed at startup. Trade the token for a
        # session cookie and redirect, so it doesn't linger in the address bar.
        return _sign_in(request) if auth.check_token(token) else _login_page("That token is not valid.")

    if not auth.is_authenticated(request):
        return _login_page()

    template = templates.get_template("index.html")
    html_content = template.render(request=request)
    return HTMLResponse(content=html_content)


@app.post("/login")
async def login(request: Request, token: str = Form("")):
    """Sign in with the access token."""
    if request.app.state.auth.check_token(token):
        return _sign_in(request)
    return _login_page("That token is not valid.")


@app.post("/logout")
async def logout(request: Request):
    """End this browser session."""
    request.app.state.auth.close_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"status": "signed out"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    """Delete a job's uploaded file, outputs and mapping right away."""
    _get_job(job_id)
    _drop_job(job_id)
    return JSONResponse({"status": "deleted"})


@app.post("/api/detect")
async def detect_pii(
    file: UploadFile = File(...),
    match_strategy: str = Form("normalized"),
    extra_columns: str = Form(""),
    override_defaults: bool = Form(False),
):
    """Detect PII columns in uploaded file."""
    temp_dir = None
    try:
        # Save uploaded file temporarily
        job_id = str(uuid.uuid4())
        temp_dir = _new_job_dir("pii_faker_")
        filename = _safe_filename(file.filename, "upload")
        input_path = temp_dir / filename

        with open(input_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Create config
        extra_cols = [c.strip() for c in extra_columns.split(',') if c.strip()]
        config = PIIConfig(
            match_strategy=MatchStrategy(match_strategy),
            extra_pii_columns=extra_cols,
            override_defaults=override_defaults,
        )

        # Read and detect
        file_handler = FileHandler(config)
        data = file_handler.read(str(input_path))

        detector = PIIColumnDetector(config)

        if isinstance(data, dict):
            # Multiple sheets
            all_detections = {}
            for sheet_name, df in data.items():
                all_detections[sheet_name] = _detect_sheet(df, detector, config)
        else:
            all_detections = {
                "default": _detect_sheet(data, detector, config)
            }

        # Store job info
        _store_job(
            job_id,
            input_path=str(input_path),
            temp_dir=str(temp_dir),
            filename=filename,
            config=config.model_dump(),
            sheets=list(all_detections.keys()),
        )

        return JSONResponse({
            "job_id": job_id,
            "detections": all_detections,
            "filename": filename,
        })

    except Exception as e:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)    # don't leave the upload behind
        raise HTTPException(status_code=400, detail=str(e))


def _selected_entries(df, columns: List[str], config: PIIConfig) -> List[Dict[str, Any]]:
    """
    Turn the fields ticked in the UI into entries for PIIProcessor.

    A key is a column name, a nested path (`patient.contacts[*].email`), or
    `column:json.path` for a JSON document stored in a text column.
    """
    detected = {e['column']: e for e in PIIColumnDetector(config).detect_all(df)}

    entries = []
    for key in columns:
        if key in detected:
            entries.append(detected[key])
        elif ':' in key and key not in df.columns:
            column, path = key.split(':', 1)
            path = re.sub(r'(\[\*\])+$', '', path)
            if column in df.columns and JsonWalker.segments(path):
                entries.append({
                    'column': f"{column}:{path}",
                    'json_column': column,
                    'json_path': path,
                    'category': _get_category_for_field(JsonWalker.segments(path)[-1], config),
                    'pattern': path,
                    'description': 'Selected in the web UI',
                })
        else:
            segments = JsonWalker.segments(key, df.schema)
            if segments and segments[0] in df.columns:
                entries.append({
                    'column': key,
                    'category': _get_category_for_field(segments[-1], config),
                    'pattern': key,
                    'description': 'Selected in the web UI',
                })
    return entries


def _get_category_for_field(field_name, config):
    """Get PII category for a field name."""
    detector = PIIColumnDetector(config)
    match = detector._match_column(field_name)
    if match:
        return match['category']
    return "custom"


def _detect_sheet(df, detector, config):
    """Detect PII in a single sheet, including nested structures."""
    detections = []
    detected_paths = set()

    # Detect flat columns
    pii_cols = detector.detect(df)
    for col in pii_cols:
        detections.append({
            "column": col['column'],
            "category": col['category'],
            "unique_count": int(df[col['column']].n_unique()),
            "type": "flat",
            "path": col['column'],
        })
        detected_paths.add(col['column'])

    # Detect struct columns (nested)
    for col_name, col_type in df.schema.items():
        if isinstance(col_type, pl.Struct):
            nested_pii = _detect_nested_in_struct(df, col_name, col_type, detector)
            for item in nested_pii:
                if item['path'] not in detected_paths:
                    detections.append(item)
                    detected_paths.add(item['path'])
        elif isinstance(col_type, pl.List) and isinstance(col_type.inner, pl.Struct):
            nested_pii = _detect_nested_in_list(df, col_name, col_type.inner, detector)
            for item in nested_pii:
                if item['path'] not in detected_paths:
                    detections.append(item)
                    detected_paths.add(item['path'])

    # Detect JSON string columns
    for col_name in df.columns:
        if col_name in detected_paths:
            continue
        if _is_json_column(df, col_name):
            json_pii = _detect_json_string_column(df, col_name, detector)
            for item in json_pii:
                if item['path'] not in detected_paths:
                    detections.append(item)
                    detected_paths.add(item['path'])

    # Handle extra columns with nested paths (e.g., data.tanggal_lahir)
    extra_cols = config.extra_pii_columns
    for extra in extra_cols:
        if extra in detected_paths:
            continue
        # Check if it's a nested path
        if '.' in extra or '[*]' in extra:
            result = _detect_extra_nested_path(df, extra, detector)
            if result and result['path'] not in detected_paths:
                detections.append(result)
                detected_paths.add(result['path'])

    return detections


def _detect_extra_nested_path(df, path, detector):
    """Detect a specific nested path provided by user."""
    import re

    parts = path.split('.')
    if not parts:
        return None

    # Try to find the path in the schema
    current_type = None
    current_path = []
    parent = None

    for i, part in enumerate(parts):
        clean_part = part.replace('[*]', '')

        if i == 0:
            # Top-level column
            if clean_part not in df.schema:
                return None
            current_type = df.schema[clean_part]
            current_path.append(clean_part)
            parent = clean_part
        else:
            # Nested field
            if isinstance(current_type, pl.Struct):
                field_found = False
                for field in current_type.fields:
                    if field.name == clean_part:
                        current_type = field.dtype
                        current_path.append(clean_part)
                        field_found = True
                        break
                if not field_found:
                    return None
            elif isinstance(current_type, pl.List) and isinstance(current_type.inner, pl.Struct):
                # Handle [*] notation
                field_found = False
                for field in current_type.inner.fields:
                    if field.name == clean_part:
                        current_type = field.dtype
                        current_path.append(clean_part)
                        field_found = True
                        break
                if not field_found:
                    return None
            else:
                return None

    # Get the last part as the field name
    field_name = parts[-1].replace('[*]', '')
    match = detector._match_column(field_name)

    if not match:
        # If no match by default, treat as custom
        match = {
            'category': 'custom',
            'original': field_name,
            'description': f'User-defined: {path}'
        }

    return {
        "column": field_name,
        "category": match['category'],
        "unique_count": 0,
        "type": "nested_array" if '[*]' in path else "nested",
        "path": path,
        "parent": parent,
    }


def _detect_nested_in_struct(df, col_name, struct_type, detector):
    """Detect PII in a struct column."""
    detections = []
    for field in struct_type.fields:
        field_path = f"{col_name}.{field.name}"
        match = detector._match_column(field.name)
        if match:
            # Count unique values in the nested field
            try:
                unique_count = len(df[col_name].struct.field(field.name).drop_nulls().unique())
            except:
                unique_count = 0
            detections.append({
                "column": field.name,
                "category": match['category'],
                "unique_count": unique_count,
                "type": "nested",
                "path": field_path,
                "parent": col_name,
            })
        # Recurse if nested field is also a struct
        if isinstance(field.dtype, pl.Struct):
            sub_detections = _detect_nested_in_struct(
                df, field_path, field.dtype, detector
            )
            detections.extend(sub_detections)
        # Recurse into list of structs
        elif isinstance(field.dtype, pl.List) and isinstance(field.dtype.inner, pl.Struct):
            sub_detections = _detect_nested_in_list(
                df, field_path, field.dtype.inner, detector
            )
            detections.extend(sub_detections)
    return detections


def _detect_nested_in_list(df, col_name, inner_struct, detector):
    """Detect PII in a list of structs column."""
    detections = []
    for field in inner_struct.fields:
        field_path = f"{col_name}[*].{field.name}"
        match = detector._match_column(field.name)
        if match:
            detections.append({
                "column": field.name,
                "category": match['category'],
                "unique_count": 0,  # Hard to count in lists
                "type": "nested_array",
                "path": field_path,
                "parent": col_name,
            })
    return detections


def _is_json_column(df, col_name, sample_size=10):
    """Check if a column contains JSON strings."""
    try:
        sample = df[col_name].drop_nulls().head(sample_size).to_list()
        if not sample:
            return False
        json_count = 0
        for val in sample:
            if isinstance(val, str) and val.strip().startswith(('{', '[')):
                try:
                    json_module.loads(val)
                    json_count += 1
                except:
                    pass
        return json_count >= len(sample) * 0.5  # At least50% are valid JSON
    except:
        return False


def _detect_json_string_column(df, col_name, detector):
    """Detect PII fields inside JSON string column."""
    detections = []
    try:
        # Try to parse JSON and find PII keys
        sample = df[col_name].drop_nulls().head(10).to_list()
        if not sample:
            return detections

        # Parse first valid JSON to get structure
        for val in sample:
            if isinstance(val, str):
                try:
                    parsed = json_module.loads(val)
                    if isinstance(parsed, dict):
                        _find_pii_in_dict(parsed, col_name, "", detector, detections)
                    break
                except:
                    continue

    except:
        pass

    return detections


def _find_pii_in_dict(data, json_col, path, detector, detections):
    """Recursively find PII keys in a dictionary."""
    for key, value in data.items():
        current_path = f"{path}.{key}" if path else key
        full_path = f"{json_col}:{current_path}"

        match = detector._match_column(key)
        if match:
            detections.append({
                "column": key,
                "category": match['category'],
                "unique_count": 0,
                "type": "json_nested",
                "path": full_path,
                "parent": json_col,
                "json_path": current_path,
            })

        # Recurse into nested dicts
        if isinstance(value, dict):
            _find_pii_in_dict(value, json_col, current_path, detector, detections)
        elif isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict):
            _find_pii_in_dict(value[0], json_col, f"{current_path}[*]", detector, detections)


@app.post("/api/process")
async def process_file(
    request: Request,
    job_id: str = Form(...),
    selected_columns: str = Form(...),
    mode: str = Form("fake"),
    redact_style: str = Form("full"),
    seed: Optional[int] = Form(None),
):
    """Process file with selected PII columns."""
    job = _get_job(job_id)
    input_path = job["input_path"]
    temp_dir = Path(job["temp_dir"])

    try:
        # Parse selected columns
        columns = [c.strip() for c in selected_columns.split(',') if c.strip()]

        # Update config
        config = PIIConfig(**job["config"])
        config.mode = PIIMode(mode)
        config.redact_style = RedactionStyle(redact_style)
        config.seed = seed

        # Read file
        file_handler = FileHandler(config)
        data = file_handler.read(input_path)

        # One processor for the whole file: a value gets the same replacement
        # in every column and sheet, and every replacement is distinct.
        processor = PIIProcessor(config)

        if isinstance(data, dict):
            processed = {
                sheet_name: processor.process(df, _selected_entries(df, columns, config))
                for sheet_name, df in data.items()
            }
        else:
            processed = processor.process(data, _selected_entries(data, columns, config))

        # Save the mapping first, so a failure never leaves output we can't restore
        output_path = temp_dir / f"processed_{job['filename']}"
        mapping_path = temp_dir / f"mapping_{job_id}.json"
        processor.mapper.save(str(mapping_path))

        if isinstance(processed, dict):
            for sheet_name, df in processed.items():
                sheet_output = temp_dir / f"processed_{sheet_name}_{job['filename']}"
                file_handler.write(df, str(sheet_output))
        else:
            file_handler.write(processed, str(output_path))

        # Update job
        job["output_path"] = str(output_path)
        job["mapping_path"] = str(mapping_path)
        job["status"] = "completed"

        return JSONResponse({
            "job_id": job_id,
            "status": "completed",
            "output_filename": f"processed_{job['filename']}",
            "mapping_filename": f"mapping_{job_id}.json",
            # Selected fields that held no text, so were left as they were
            "skipped": processor.skipped,
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download/{job_id}/{file_type}")
async def download_file(job_id: str, file_type: str):
    """Download processed file or mapping."""
    job = _get_job(job_id)

    if file_type == "output":
        file_path = job.get("output_path")
        filename = f"processed_{job['filename']}"
    elif file_type == "mapping":
        file_path = job.get("mapping_path")
        filename = f"mapping_{job_id}.json"
    else:
        raise HTTPException(status_code=400, detail="Invalid file type")

    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        file_path,
        filename=filename,
        media_type="application/octet-stream",
    )


@app.post("/api/restore")
async def restore_file(
    request: Request,
    mapping_file: UploadFile = File(...),
    input_file: UploadFile = File(...),
    is_text: bool = Form(False),
):
    """Restore original values from faked/redacted data."""
    temp_dir = None
    try:
        # Save files temporarily
        temp_dir = _new_job_dir("pii_faker_restore_")
        input_name = _safe_filename(input_file.filename, "input")

        # Prefixed so the two uploads can't overwrite each other when they share a name
        mapping_path = temp_dir / f"mapping_{_safe_filename(mapping_file.filename, 'mapping.json')}"
        input_path = temp_dir / f"input_{input_name}"

        with open(mapping_path, "wb") as f:
            f.write(await mapping_file.read())
        with open(input_path, "wb") as f:
            f.write(await input_file.read())

        # Load mapping
        mapper = PIIMapper.load(str(mapping_path))
        restorer = PIIRestorer(mapper)

        if is_text:
            with open(input_path, 'r') as f:
                text = f.read()
            restored_text = restorer.restore_text(text)
            output_path = temp_dir / "restored.txt"
            with open(output_path, 'w') as f:
                f.write(restored_text)
        else:
            config = mapper.config
            file_handler = FileHandler(config)
            df = file_handler.read(str(input_path))

            if isinstance(df, dict):
                for sheet_name, sheet_df in df.items():
                    restored_df = restorer.restore_dataframe(sheet_df)
                    output_path = temp_dir / f"restored_{sheet_name}_{input_name}"
                    file_handler.write(restored_df, str(output_path))
            else:
                restored_df = restorer.restore_dataframe(df)
                output_path = temp_dir / f"restored_{input_name}"
                file_handler.write(restored_df, str(output_path))

        # Store for download
        restore_job_id = str(uuid.uuid4())
        _store_job(
            restore_job_id,
            output_path=str(output_path),
            temp_dir=str(temp_dir),
        )

        return JSONResponse({
            "job_id": restore_job_id,
            "status": "completed",
            "output_filename": f"restored_{input_name}",
        })

    except Exception as e:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)    # don't leave the uploads behind
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/restore/{job_id}")
async def download_restore(job_id: str):
    """Download restored file."""
    job = _get_job(job_id)
    output_path = job.get("output_path")

    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        output_path,
        filename=Path(output_path).name,
        media_type="application/octet-stream",
    )


def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    token: Optional[str] = None,
    retention_minutes: float = DEFAULT_RETENTION_MINUTES,
    announce: Callable[[str], None] = print,
) -> None:
    """
    Start the web UI.

    Args:
        token: Access token (min. 16 characters). Defaults to $PII_FAKER_TOKEN,
            or a random one that is printed so you can open the link.
        retention_minutes: How long a job's files are kept without activity.
        announce: Where startup messages go.
    """
    import uvicorn

    app.state.auth = Authenticator(token or os.environ.get(TOKEN_ENV) or None)
    app.state.retention_seconds = int(retention_minutes * 60)

    shown_host = "localhost" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown_host}:{port}"
    if app.state.auth.generated:
        announce(f"Open this link to sign in: {url}/?token={app.state.auth.token}")
    else:
        announce(f"Open {url} and sign in with your access token.")

    if host not in ("127.0.0.1", "localhost", "::1"):
        announce(
            f"Warning: listening on {host} over plain HTTP, so the token and your data travel "
            "unencrypted. Put a TLS-terminating proxy in front before using this on a network."
        )
    unit = "minute" if retention_minutes == 1 else "minutes"
    announce(
        f"Uploads, outputs and mappings are deleted after {retention_minutes:g} {unit} "
        "without activity, and when the server stops."
    )

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
