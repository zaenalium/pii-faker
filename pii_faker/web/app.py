"""
PII Faker Web Application

FastAPI-based web interface for PII processing.
Provides file upload, detection preview, processing, and restore functionality.
"""

import asyncio
import os
import time
import uuid
import tempfile
import shutil
import json as json_module
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict, Any

import polars as pl
from fastapi import Depends, FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle, MatchStrategy
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.restorer import PIIRestorer
from pii_faker.core.file_io import FileHandler
from pii_faker.core.pipeline import detect_targets, process_sheets, restore_sheets
from pii_faker.web.auth import TOKEN_ENV, SESSION_COOKIE, Authenticator, session_cookie_options

MAX_UPLOAD_BYTES = 500 * 1024 * 1024
DEFAULT_RETENTION_MINUTES = 60


class JobStore(dict):
    """The job dict, remembering when each job was last used so idle jobs can expire.

    Any lookup counts as activity, so the endpoints need no changes.
    """

    def __init__(self):
        super().__init__()
        self.touched: Dict[str, float] = {}

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.touched[key] = time.time()
        return value

    def get(self, key, default=None):
        return self[key] if key in self else default

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.touched[key] = time.time()

    def pop(self, key, *default):
        self.touched.pop(key, None)
        return super().pop(key, *default)


def purge_expired_jobs(max_age_seconds: float, now: Optional[float] = None) -> int:
    """Delete jobs (and their temp files) idle for longer than `max_age_seconds`."""
    now = time.time() if now is None else now
    expired = [job_id for job_id, touched in list(jobs.touched.items()) if now - touched > max_age_seconds]
    for job_id in expired:
        _cleanup_job(job_id)
    return len(expired)


def purge_all_jobs() -> None:
    """Delete every job's temp files (server shutdown)."""
    for job_id in list(jobs):
        _cleanup_job(job_id)


async def _reap_expired_jobs(application: FastAPI) -> None:
    while True:
        retention = application.state.retention_seconds
        if not retention:                       # the desktop app keeps jobs until it closes
            await asyncio.sleep(3600)
            continue
        await asyncio.sleep(max(1.0, min(60.0, retention / 2)))
        purge_expired_jobs(retention)


@asynccontextmanager
async def lifespan(application: FastAPI):
    reaper = asyncio.create_task(_reap_expired_jobs(application))
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

# Setup static files and templates with absolute paths
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Ensure directories exist
STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# In-memory job storage (single process; production should use Redis/DB)
jobs = JobStore()

# Never open by default: a token is generated unless run_server() is given one.
app.state.auth = Authenticator()
app.state.retention_seconds = DEFAULT_RETENTION_MINUTES * 60
# Reading and writing arbitrary local paths is for the single-user desktop app only.
app.state.allow_local_paths = False

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


def _require_local_paths() -> None:
    if not app.state.allow_local_paths:
        raise HTTPException(
            status_code=403,
            detail="Path-based processing reads and writes local files and is only available in the desktop app.",
        )


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
        # Opened from the link printed at startup. Trade the token for a session
        # cookie and redirect, so it doesn't linger in the address bar.
        return _sign_in(request) if auth.check_token(token) else _login_page("That token is not valid.")

    if not auth.is_authenticated(request):
        return _login_page()

    try:
        from pii_faker.core.config import auto_expire_mappings
        auto_expire_mappings()
    except Exception:
        pass
    template = templates.get_template("index.html")
    session_token = os.environ.get("PII_FAKER_SESSION_TOKEN", "")
    session_token = getattr(app.state, "session_token", "") or session_token
    html_content = template.render(request=request, session_token=session_token)
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
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    _cleanup_job(job_id)
    return JSONResponse({"status": "deleted"})


@app.post("/api/detect")
async def detect_pii(
    file: UploadFile = File(...),
    match_strategy: str = Form("normalized"),
    extra_columns: str = Form(""),
    override_defaults: bool = Form(False),
):
    """Detect PII columns in an uploaded file (via the single pipeline plan)."""
    job_id = str(uuid.uuid4())
    temp_dir = Path(tempfile.mkdtemp(prefix="pii_faker_"))
    try:
        # Save uploaded file temporarily (sanitised name, size-capped)
        filename = _safe_filename(file.filename)
        input_path = temp_dir / filename
        await _save_upload(file, input_path)
        return _detect_path_job(
            job_id, temp_dir, input_path, filename,
            match_strategy, extra_columns, override_defaults,
        )
    except HTTPException:
        _cleanup_job(job_id)
        shutil.rmtree(temp_dir, ignore_errors=True)   # the job isn't registered yet on failure
        raise
    except Exception as e:
        _cleanup_job(job_id)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(e))


def _safe_filename(filename: str) -> str:
    """Strip path components from an upload filename (D13)."""
    name = Path(filename or "upload").name
    return name or "upload"


async def _save_upload(upload: UploadFile, dest: Path) -> int:
    """Stream an upload to disk with a size cap. Returns bytes written."""
    written = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large")
            f.write(chunk)
    return written


def _detect_path_job(
    job_id: str,
    temp_dir: Path,
    input_path: Path,
    filename: str,
    match_strategy: str = "normalized",
    extra_columns: str = "",
    override_defaults: bool = False,
) -> JSONResponse:
    """Shared detect implementation for upload and local-path flows."""
    extra_cols = [c.strip() for c in extra_columns.split(",") if c.strip()]
    config = PIIConfig(
        match_strategy=MatchStrategy(match_strategy),
        extra_pii_columns=extra_cols,
        override_defaults=override_defaults,
    )

    file_handler = FileHandler(config)
    data = file_handler.read(str(input_path))

    sheets = data if isinstance(data, dict) else {"default": data}
    all_detections = {}
    for sheet_name, df in sheets.items():
        plan = detect_targets(df, config)
        detections = []
        for t in plan.targets:
            kind = t.kind
            if kind == "nested":
                dtype = "nested_array" if "[*]" in t.path else "nested"
            elif kind == "json_string":
                dtype = "json_nested"
            else:
                dtype = "flat"
            try:
                unique_count = int(df[t.column].n_unique())
            except Exception:
                unique_count = 0
            detections.append({
                "column": t.column,
                "category": t.category,
                "unique_count": unique_count,
                "type": dtype,
                "path": t.path,
                "parent": t.column if dtype != "flat" else None,
                "json_path": t.json_path or None,
                "confidence": t.confidence,
                "name_match": t.name_match,
                "value_detector": t.value_detector,
                "value_ratio": t.value_ratio,
            })
        all_detections[sheet_name] = detections

    jobs[job_id] = {
        "input_path": str(input_path),
        "temp_dir": str(temp_dir),
        "filename": filename,
        "config": config.model_dump(),
        "sheets": list(all_detections.keys()),
        "local_path": str(input_path) if temp_dir is None else None,
    }

    from pii_faker.core.value_detectors import scan_frame_values, suggest_locale

    all_columns: list = []
    value_findings: dict = {}
    for sheet_name, df in sheets.items():
        all_columns.extend(df.columns)
        for col, matches in scan_frame_values(df).items():
            value_findings[f"{sheet_name}.{col}"] = matches

    return JSONResponse({
        "job_id": job_id,
        "detections": all_detections,
        "filename": filename,
        "suggested_locale": suggest_locale(value_findings, all_columns),
    })


from pydantic import BaseModel


@app.get("/api/presets")
async def list_presets_api():
    """List compliance presets (roadmap 3.4)."""
    from pii_faker.core.presets import list_presets

    return JSONResponse(list_presets())


class DeleteMappingBody(BaseModel):
    """Request body for deleting a job's server-side mapping file."""

    job_id: str


@app.post("/api/delete-mapping")
async def delete_mapping(body: DeleteMappingBody):
    """Delete a job's mapping file from the server (1.5 one-click delete)."""
    job = jobs.get(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    mapping_path = job.pop("mapping_path", None)
    deleted = False
    if mapping_path:
        try:
            Path(mapping_path).unlink(missing_ok=True)
            deleted = True
        except Exception:
            deleted = False
    return JSONResponse({"job_id": body.job_id, "deleted": deleted})


class DetectPathBody(BaseModel):
    """Request body for local-path detection (desktop path-based flow)."""

    path: str
    match_strategy: str = "normalized"
    extra_columns: str = ""
    override_defaults: bool = False


class ProcessPathBody(BaseModel):
    """Request body for local-path processing (desktop path-based flow)."""

    job_id: str
    selected_columns: str = ""
    mode: str = "fake"
    redact_style: str = "full"
    seed: Optional[int] = None
    locale: Optional[str] = None
    preset: Optional[str] = None
    output_path: Optional[str] = None
    mapping_path: Optional[str] = None


def _resolve_local_path(raw: str) -> Path:
    """Validate a desktop-supplied local path (must exist, supported type)."""
    candidate = Path(raw).expanduser().resolve()
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=400, detail="Path does not exist")
    handler_exts = set(FileHandler.SUPPORTED_EXTENSIONS)
    if candidate.suffix.lower() not in handler_exts:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {candidate.suffix}",
        )
    return candidate


@app.post("/api/detect-path", dependencies=[Depends(_require_local_paths)])
async def detect_path(body: DetectPathBody):
    """Detect PII in a local file by path (desktop only, no upload copy)."""
    job_id = str(uuid.uuid4())
    try:
        input_path = _resolve_local_path(body.path)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        return _detect_path_job(
            job_id, None, input_path, input_path.name,
            body.match_strategy, body.extra_columns, body.override_defaults,
        )
    except HTTPException:
        jobs.pop(job_id, None)
        raise
    except Exception as e:
        jobs.pop(job_id, None)
        raise HTTPException(status_code=400, detail=str(e))


def _cleanup_job(job_id: str) -> None:
    """Delete a job's temp dir and forget the job (D12)."""
    job = jobs.pop(job_id, None)
    if job and job.get("temp_dir"):
        shutil.rmtree(job["temp_dir"], ignore_errors=True)


@app.post("/api/process")
async def process_file(
    request: Request,
    job_id: str = Form(...),
    selected_columns: str = Form(...),
    mode: str = Form("fake"),
    redact_style: str = Form("full"),
    seed: Optional[int] = Form(None),
    locale: Optional[str] = Form(None),
):
    """Process file with selected PII columns (single pipeline)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    input_path = job["input_path"]
    temp_dir = job.get("temp_dir")

    try:
        # Parse selected columns (pipeline dotted paths, incl. parent:json.path)
        columns = [c.strip() for c in selected_columns.split(',') if c.strip()]

        # Update config
        config = PIIConfig(**job["config"])
        config.mode = PIIMode(mode)
        config.redact_style = RedactionStyle(redact_style)
        config.seed = seed
        if locale:
            config.locale = locale

        # Read file
        file_handler = FileHandler(config)
        data = file_handler.read(input_path)
        sheets = data if isinstance(data, dict) else {"default": data}

        # Process all sheets through the single pipeline (shared RunState,
        # scoped mappings, same fake for the same original everywhere)
        processed, mapper, run_state = process_sheets(sheets, config, columns)

        # Save output: next to the source file for local-path jobs so the
        # desktop app never copies large files through a temp dir.
        if temp_dir:
            out_dir = Path(temp_dir)
        else:
            out_dir = Path(input_path).parent
        output_path = out_dir / f"processed_{job['filename']}"
        mapping_path = out_dir / f"mapping_{job_id}.json"

        if len(processed) == 1 and "default" in processed:
            file_handler.write(processed["default"], str(output_path))
        else:
            for sheet_name, df in processed.items():
                sheet_output = out_dir / f"processed_{sheet_name}_{job['filename']}"
                file_handler.write(df, str(sheet_output))

        mapper.save(str(mapping_path))

        # Update job
        job["output_path"] = str(output_path)
        job["mapping_path"] = str(mapping_path)
        job["status"] = "completed"
        job["warnings"] = run_state.warnings
        # Expose PII-free audit (3.3) for GET /api/download/{job_id}/audit
        job["audit"] = run_state.audit.to_dict() if hasattr(run_state, "audit") and run_state.audit else None
        job["audit_json"] = str(mapping_path).replace(".json", "_audit.json").replace(".db", "_audit.json") if hasattr(run_state, "audit") and run_state.audit else None

        return JSONResponse({
            "job_id": job_id,
            "status": "completed",
            "output_filename": f"processed_{job['filename']}",
            "mapping_filename": f"mapping_{job_id}.json",
            "warnings": list(dict.fromkeys(run_state.warnings)),
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/process-path", dependencies=[Depends(_require_local_paths)])
async def process_path(body: ProcessPathBody):
    """Process a local file by path (desktop only, no upload copy)."""
    if body.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[body.job_id]
    if not job.get("local_path"):
        raise HTTPException(
            status_code=400, detail="Job is not a local-path job"
        )
    try:
        out_dir: Optional[Path] = None
        if body.output_path:
            out_path = Path(body.output_path).expanduser()
            out_dir = out_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            job["_forced_output"] = str(out_path)
        if body.mapping_path:
            map_path = Path(body.mapping_path).expanduser()
            map_path.parent.mkdir(parents=True, exist_ok=True)
            job["_forced_mapping"] = str(map_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        columns = [c.strip() for c in body.selected_columns.split(",") if c.strip()]
        config = PIIConfig(**job["config"])
        config.mode = PIIMode(body.mode)
        config.redact_style = RedactionStyle(body.redact_style)
        config.seed = body.seed
        if body.locale:
            config.locale = body.locale

        file_handler = FileHandler(config)
        data = file_handler.read(job["input_path"])
        sheets = data if isinstance(data, dict) else {"default": data}
        processed, mapper, run_state = process_sheets(sheets, config, columns)

        out_dir = Path(job["input_path"]).parent
        output_path = Path(job.pop("_forced_output", str(out_dir / f"processed_{job['filename']}")))
        mapping_path = Path(job.pop("_forced_mapping", str(out_dir / f"mapping_{body.job_id}.json")))
        if len(processed) == 1 and "default" in processed:
            file_handler.write(processed["default"], str(output_path))
        else:
            for sheet_name, df in processed.items():
                sheet_output = output_path.parent / f"{output_path.stem}_{sheet_name}{output_path.suffix}"
                file_handler.write(df, str(sheet_output))

        mapper.save(str(mapping_path))

        job["output_path"] = str(output_path)
        job["mapping_path"] = str(mapping_path)
        job["status"] = "completed"
        job["warnings"] = run_state.warnings
        job["audit"] = run_state.audit.to_dict() if hasattr(run_state, "audit") and run_state.audit else None
        job["audit_json"] = str(mapping_path).replace(".json", "_audit.json").replace(".db", "_audit.json") if hasattr(run_state, "audit") and run_state.audit else None

        return JSONResponse({
            "job_id": body.job_id,
            "status": "completed",
            "output_path": str(output_path),
            "mapping_path": str(mapping_path),
            "warnings": list(dict.fromkeys(run_state.warnings)),
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download/{job_id}/{file_type}")
async def download_file(job_id: str, file_type: str):
    """Download processed file, mapping, or audit (temp dir deleted afterwards for files)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

    if file_type == "audit":
        audit = job.get("audit")
        if audit is None:
            audit_json = job.get("audit_json")
            if audit_json and Path(audit_json).exists():
                return FileResponse(audit_json, filename=f"audit_{job_id}.json", media_type="application/json")
            raise HTTPException(status_code=404, detail="Audit not found")
        return JSONResponse(audit)

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
        background=BackgroundTask(_cleanup_job, job_id),
    )


@app.post("/api/restore")
async def restore_file(
    request: Request,
    mapping_file: UploadFile = File(...),
    input_file: UploadFile = File(...),
    is_text: bool = Form(False),
):
    """Restore original values from faked/redacted data."""
    temp_dir = Path(tempfile.mkdtemp(prefix="pii_faker_restore_"))
    try:
        # Save files temporarily (sanitised names, size-capped)
        mapping_path = temp_dir / _safe_filename(mapping_file.filename)
        input_path = temp_dir / _safe_filename(input_file.filename)

        await _save_upload(mapping_file, mapping_path)
        await _save_upload(input_file, input_path)

        # Load mapping
        mapper = PIIMapper.load(str(mapping_path))
        restorer = PIIRestorer(mapper)

        if is_text:
            with open(input_path, 'r') as f:
                text = f.read()
            restored_text, spans = restorer.restore_text_with_spans(text, tolerant=True)
            output_path = temp_dir / "restored.txt"
            with open(output_path, 'w') as f:
                f.write(restored_text)
            span_payload = [
                {"start": s, "end": e, "original": o}
                for (s, e, o) in spans
            ]
        else:
            span_payload = None
            config = mapper.config
            file_handler = FileHandler(config)
            df = file_handler.read(str(input_path))

            if isinstance(df, dict):
                restored = restore_sheets(df, mapper)
                for sheet_name, restored_df in restored.items():
                    output_path = temp_dir / f"restored_{sheet_name}_{_safe_filename(input_file.filename)}"
                    file_handler.write(restored_df, str(output_path))
            else:
                restored_df = restorer.restore_dataframe(df)
                output_path = temp_dir / f"restored_{_safe_filename(input_file.filename)}"
                file_handler.write(restored_df, str(output_path))

        # Store for download
        restore_job_id = str(uuid.uuid4())
        jobs[restore_job_id] = {
            "output_path": str(output_path),
            "temp_dir": str(temp_dir),
        }

        return JSONResponse({
            "job_id": restore_job_id,
            "status": "completed",
            "output_filename": f"restored_{_safe_filename(input_file.filename)}",
            "spans": span_payload,
        })

    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/restore/{job_id}")
async def download_restore(job_id: str):
    """Download restored file (temp dir deleted afterwards)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    output_path = job.get("output_path")

    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        output_path,
        filename=Path(output_path).name,
        media_type="application/octet-stream",
        background=BackgroundTask(_cleanup_job, job_id),
    )


@app.post("/api/restore-text")
async def restore_text_api(
    mapping_file: UploadFile = File(...),
    text: str = Form(...),
    tolerant: bool = Form(True),
    passphrase: Optional[str] = Form(None),
):
    """Restore pasted free-form text, returning restored text + spans (4.1/4.3)."""
    temp_dir = Path(tempfile.mkdtemp(prefix="pii_faker_rt_"))
    try:
        mapping_path = temp_dir / _safe_filename(mapping_file.filename)
        await _save_upload(mapping_file, mapping_path)
        mapper = PIIMapper.load(str(mapping_path), passphrase=passphrase)
        restorer = PIIRestorer(mapper)
        restored, spans = restorer.restore_text_with_spans(text, tolerant=tolerant)
        return JSONResponse({
            "restored": restored,
            "spans": [{"start": s, "end": e, "original": o} for (s, e, o) in spans],
            "ambiguous": getattr(restorer, "ambiguous_hits", []),
        })
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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
