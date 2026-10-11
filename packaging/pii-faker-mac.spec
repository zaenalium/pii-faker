# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the PII Faker macOS desktop app (roadmap 1.6)."""

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
    exclude_binaries=True,
    name="PII Faker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PII Faker",
)

app = BUNDLE(
    coll,
    name="PII Faker.app",
    icon=None,
    bundle_identifier="ai.commandcode.pii-faker",
    info_plist={
        "CFBundleShortVersionString": "0.4.0",
        "CFBundleVersion": "0.4.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
