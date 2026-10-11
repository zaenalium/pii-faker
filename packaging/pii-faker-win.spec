# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the PII Faker Windows desktop app (roadmap 1.7, 1.9)."""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
PKG = ROOT / "pii_faker"

datas = [
    (str(PKG / "data" / "default_pii_columns.yaml"), "pii_faker/data"),
    (str(PKG / "web" / "templates"), "pii_faker/web/templates"),
]
datas += collect_data_files("faker", include_py_files=False)
datas += collect_data_files("pywebview", include_py_files=False)

hiddenimports = (
    collect_submodules("pii_faker")
    + collect_submodules("faker")
    + [
        "uvicorn",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "fastapi",
        "starlette",
        "pydantic",
        "typer",
        "rich",
        "yaml",
        "jinja2",
        "multipart",
        "xlsxwriter",
        "fastexcel",
        "polars",
        "platformdirs",
        "webview",
    ]
)

a = Analysis(
    [str(PKG / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "scipy", "pytest", "hypothesis",
        "torch", "torchvision", "torchaudio", "torchgen", "torchrec",
        "torch_xla", "torcharrow", "torchcomms", "torchcodec",
        "torchdistx", "functorch",
        "onnxruntime", "onnx",
        "pandas", "geopandas", "modin",
        "duckdb",
        "pyarrow",
        "PySide6", "PyQt5", "PyQt6",
        "ipykernel", "IPython", "jupyter",
        "altair",
        "polars.ml.torch",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PII-Faker.exe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
