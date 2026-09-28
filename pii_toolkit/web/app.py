"""
PII Toolkit Web Application

FastAPI-based web interface for PII processing.
Provides file upload, detection preview, processing, and restore functionality.
"""

import uuid
import tempfile
import json as json_module
from pathlib import Path
from typing import Optional, Dict, Any

import polars as pl
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pii_toolkit.core.config import PIIConfig, PIIMode, RedactionStyle, MatchStrategy
from pii_toolkit.core.detector import PIIColumnDetector
from pii_toolkit.core.generators import PIIGenerator
from pii_toolkit.core.redactor import PIIRedactor
from pii_toolkit.core.mapper import PIIMapper
from pii_toolkit.core.restorer import PIIRestorer
from pii_toolkit.core.file_io import FileHandler

app = FastAPI(
    title="PII Toolkit",
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
    """Detect PII columns in uploaded file."""
    try:
        # Save uploaded file temporarily
        job_id = str(uuid.uuid4())
        temp_dir = Path(tempfile.mkdtemp(prefix="pii_toolkit_"))
        input_path = temp_dir / file.filename

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
        jobs[job_id] = {
            "input_path": str(input_path),
            "temp_dir": str(temp_dir),
            "filename": file.filename,
            "config": config.model_dump(),
            "sheets": list(all_detections.keys()),
        }

        return JSONResponse({
            "job_id": job_id,
            "detections": all_detections,
            "filename": file.filename,
        })

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _process_sheet(df, flat_columns, json_columns, config, mapper):
    """Process a single sheet, handling both flat and JSON string columns."""
    # Process flat columns
    if flat_columns:
        detector = PIIColumnDetector(config)
        pii_cols = [c for c in detector.detect(df) if c['column'] in flat_columns]

        if config.mode == PIIMode.FAKE:
            generator = PIIGenerator(config)
            for col_info in pii_cols:
                df, col_mapping = generator.process_column(df, col_info)
                mapper.add_column_mapping(col_info['column'], col_info['category'], col_mapping)
        else:
            redactor = PIIRedactor(config)
            for col_info in pii_cols:
                df, col_mapping = redactor.process_column(df, col_info)
                mapper.add_column_mapping(col_info['column'], col_info['category'], col_mapping)

    # Process JSON string columns
    if json_columns:
        df = _process_json_columns(df, json_columns, config, mapper)

    return df


def _process_json_columns(df, json_columns, config, mapper):
    """Process JSON string columns to fake/redact PII inside them."""
    import re

    # Group json_columns by parent column
    # e.g., ["data:birth_date", "data:member_code"] -> {"data": ["birth_date", "member_code"]}
    json_col_groups = {}
    for col_path in json_columns:
        parts = col_path.split(':', 1)
        if len(parts) == 2:
            parent_col, json_path = parts
            if parent_col not in json_col_groups:
                json_col_groups[parent_col] = []
            json_col_groups[parent_col].append(json_path)

    # Process each parent column
    expressions = []
    for parent_col, json_paths in json_col_groups.items():
        if parent_col not in df.columns:
            continue

        # Build mapping for each json_path
        path_mappings = {}
        for json_path in json_paths:
            # Collect unique values from the JSON path
            unique_values = set()
            for val in df[parent_col].drop_nulls().to_list():
                if isinstance(val, str):
                    try:
                        parsed = json_module.loads(val)
                        value = _get_nested_value(parsed, json_path)
                        if value and isinstance(value, str):
                            unique_values.add(value)
                    except:
                        continue

            if not unique_values:
                continue

            # Generate mapping
            if config.mode == PIIMode.FAKE:
                generator = PIIGenerator(config)
                field_name = json_path.split('.')[-1]
                category = _get_category_for_field(field_name, config)
                mapping = {}
                for orig_val in sorted(unique_values):
                    mapping[orig_val] = generator.generate_fake_value(orig_val, category, field_name)
            else:
                redactor = PIIRedactor(config)
                mapping = {}
                for orig_val in sorted(unique_values):
                    if config.redact_style == RedactionStyle.FULL:
                        mapping[orig_val] = "[REDACTED]"
                    elif config.redact_style == RedactionStyle.PARTIAL:
                        if len(orig_val) > 4:
                            mapping[orig_val] = "X" * (len(orig_val) - 4) + orig_val[-4:]
                        else:
                            mapping[orig_val] = "X" * len(orig_val)
                    else:
                        mapping[orig_val] = f"REDACTED_{len(mapping)+1:04d}"

            path_mappings[json_path] = mapping

            # Add to mapper
            full_path = f"{parent_col}:{json_path}"
            mapper.add_column_mapping(full_path, category if config.mode == PIIMode.FAKE else "custom", mapping)

        if path_mappings:
            # Apply transformations using map_elements (slow path for JSON strings)
            def transform_json(val):
                if not isinstance(val, str):
                    return val
                try:
                    parsed = json_module.loads(val)
                    for json_path, mapping in path_mappings.items():
                        current = parsed
                        parts = json_path.split('.')
                        for i, part in enumerate(parts):
                            if i == len(parts) - 1:
                                if part in current and isinstance(current[part], str):
                                    if current[part] in mapping:
                                        current[part] = mapping[current[part]]
                            else:
                                if part in current:
                                    current = current[part]
                                else:
                                    break
                    return json_module.dumps(parsed, separators=(',', ':'))
                except:
                    return val

            df = df.with_columns(
                pl.col(parent_col).map_elements(transform_json, return_dtype=pl.Utf8).alias(parent_col)
            )

    return df


def _get_nested_value(data, path):
    """Get a value from nested dict using dot notation."""
    parts = path.split('.')
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


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
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
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

        # Process
        mapper = PIIMapper(config)

        # Separate columns into flat and json_nested
        flat_columns = [c for c in columns if ':' not in c]
        json_columns = [c for c in columns if ':' in c]

        if isinstance(data, dict):
            processed = {}
            for sheet_name, df in data.items():
                df = _process_sheet(df, flat_columns, json_columns, config, mapper)
                processed[sheet_name] = df
        else:
            data = _process_sheet(data, flat_columns, json_columns, config, mapper)
            processed = data

        # Save output
        output_path = temp_dir / f"processed_{job['filename']}"
        mapping_path = temp_dir / f"mapping_{job_id}.json"

        if isinstance(processed, dict):
            for sheet_name, df in processed.items():
                sheet_output = temp_dir / f"processed_{sheet_name}_{job['filename']}"
                file_handler.write(df, str(sheet_output))
        else:
            file_handler.write(processed, str(output_path))

        mapper.save(str(mapping_path))

        # Update job
        job["output_path"] = str(output_path)
        job["mapping_path"] = str(mapping_path)
        job["status"] = "completed"

        return JSONResponse({
            "job_id": job_id,
            "status": "completed",
            "output_filename": f"processed_{job['filename']}",
            "mapping_filename": f"mapping_{job_id}.json",
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download/{job_id}/{file_type}")
async def download_file(job_id: str, file_type: str):
    """Download processed file or mapping."""
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
    )


@app.post("/api/restore")
async def restore_file(
    request: Request,
    mapping_file: UploadFile = File(...),
    input_file: UploadFile = File(...),
    is_text: bool = Form(False),
):
    """Restore original values from faked/redacted data."""
    try:
        # Save files temporarily
        temp_dir = Path(tempfile.mkdtemp(prefix="pii_toolkit_restore_"))

        mapping_path = temp_dir / mapping_file.filename
        input_path = temp_dir / input_file.filename

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
                    output_path = temp_dir / f"restored_{sheet_name}_{input_file.filename}"
                    file_handler.write(restored_df, str(output_path))
            else:
                restored_df = restorer.restore_dataframe(df)
                output_path = temp_dir / f"restored_{input_file.filename}"
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
            "output_filename": f"restored_{input_file.filename}",
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/restore/{job_id}")
async def download_restore(job_id: str):
    """Download restored file."""
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
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
