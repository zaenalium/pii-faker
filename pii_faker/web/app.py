"""
PII Faker Web Application

FastAPI-based web interface for PII processing.
Provides file upload, detection preview, processing, and restore functionality.
"""

import uuid
import tempfile
import shutil
import json as json_module
from pathlib import Path
from typing import Optional, Dict, Any

import polars as pl
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle, MatchStrategy
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.restorer import PIIRestorer
from pii_faker.core.file_io import FileHandler
from pii_faker.core.pipeline import detect_targets, process_sheets, restore_sheets

MAX_UPLOAD_BYTES = 500 * 1024 * 1024

app = FastAPI(
    title="PII Faker",
    description="PII Fake/Redact Toolkit - Web Interface",
    version="0.1.0",
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

# In-memory job storage (for demo; production should use Redis/DB)
jobs: Dict[str, Dict[str, Any]] = {}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page with file upload form."""
    import os

    try:
        from pii_faker.core.config import auto_expire_mappings
        auto_expire_mappings()
    except Exception:
        pass
    template = templates.get_template("index.html")
    session_token = os.environ.get("PII_FAKER_SESSION_TOKEN", "")
    token = getattr(app.state, "session_token", "") or session_token
    html_content = template.render(request=request, session_token=token)
    return HTMLResponse(content=html_content)


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
        raise
    except Exception as e:
        _cleanup_job(job_id)
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


@app.post("/api/detect-path")
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


@app.post("/api/process-path")
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
            restored_text = restorer.restore_text(text)
            output_path = temp_dir / "restored.txt"
            with open(output_path, 'w') as f:
                f.write(restored_text)
        else:
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
