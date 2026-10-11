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
    template = templates.get_template("index.html")
    html_content = template.render(request=request)
    return HTMLResponse(content=html_content)


@app.post("/api/detect")
async def detect_pii(
    file: UploadFile = File(...),
    match_strategy: str = Form("normalized"),
    extra_columns: str = Form(""),
    override_defaults: bool = Form(False),
):
    """Detect PII columns in uploaded file (via the single pipeline plan)."""
    job_id = str(uuid.uuid4())
    temp_dir = Path(tempfile.mkdtemp(prefix="pii_faker_"))
    try:
        # Save uploaded file temporarily (sanitised name, size-capped)
        filename = _safe_filename(file.filename)
        input_path = temp_dir / filename
        await _save_upload(file, input_path)

        # Create config
        extra_cols = [c.strip() for c in extra_columns.split(',') if c.strip()]
        config = PIIConfig(
            match_strategy=MatchStrategy(match_strategy),
            extra_pii_columns=extra_cols,
            override_defaults=override_defaults,
        )

        # Read and detect via the pipeline plan (flat + nested + JSON-string)
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
                })
            all_detections[sheet_name] = detections

        # Store job info
        jobs[job_id] = {
            "input_path": str(input_path),
            "temp_dir": str(temp_dir),
            "filename": filename,
            "config": config.model_dump(),
            "sheets": list(all_detections.keys()),
        }

        return JSONResponse({
            "job_id": job_id,
            "detections": all_detections,
            "filename": filename,
        })

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
):
    """Process file with selected PII columns (single pipeline)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    input_path = job["input_path"]
    temp_dir = Path(job["temp_dir"])

    try:
        # Parse selected columns (pipeline dotted paths, incl. parent:json.path)
        columns = [c.strip() for c in selected_columns.split(',') if c.strip()]

        # Update config
        config = PIIConfig(**job["config"])
        config.mode = PIIMode(mode)
        config.redact_style = RedactionStyle(redact_style)
        config.seed = seed

        # Read file
        file_handler = FileHandler(config)
        data = file_handler.read(input_path)
        sheets = data if isinstance(data, dict) else {"default": data}

        # Process all sheets through the single pipeline (shared RunState,
        # scoped mappings, same fake for the same original everywhere)
        processed, mapper, run_state = process_sheets(sheets, config, columns)

        # Save output
        output_path = temp_dir / f"processed_{job['filename']}"
        mapping_path = temp_dir / f"mapping_{job_id}.json"

        if len(processed) == 1 and "default" in processed:
            file_handler.write(processed["default"], str(output_path))
        else:
            for sheet_name, df in processed.items():
                sheet_output = temp_dir / f"processed_{sheet_name}_{job['filename']}"
                file_handler.write(df, str(sheet_output))

        mapper.save(str(mapping_path))

        # Update job
        job["output_path"] = str(output_path)
        job["mapping_path"] = str(mapping_path)
        job["status"] = "completed"
        job["warnings"] = run_state.warnings

        return JSONResponse({
            "job_id": job_id,
            "status": "completed",
            "output_filename": f"processed_{job['filename']}",
            "mapping_filename": f"mapping_{job_id}.json",
            "warnings": list(dict.fromkeys(run_state.warnings)),
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download/{job_id}/{file_type}")
async def download_file(job_id: str, file_type: str):
    """Download processed file or mapping (temp dir deleted afterwards)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

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
