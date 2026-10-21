"""
Compliance presets (roadmap 3.4).

Each preset is a YAML file in data/presets/{name}.yaml:

    name: id
    description: "Indonesia (UU PDP) — ..."
    locale: id_ID
    columns:
      - pattern: nik
        category: identifier

Resolution merges preset columns into PIIConfig.extra_pii_columns and sets
locale only when the caller didn't already set one; the chosen preset name
is persisted in PIIConfig.preset for audit traceability.
"""

from __future__ import annotations

import importlib.resources as resources
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from pii_faker.core.config import PIIConfig


def _presets_dir() -> Path:
    # Try importlib.resources first (installed package), fall back to source tree.
    for pkg in ("pii_faker.data.presets", "pii_faker.data"):
        try:
            p = Path(str(resources.files(pkg)))
            cand = p if pkg.endswith("presets") else p / "presets"
            if cand.is_dir() or p.is_dir():
                return cand if cand.is_dir() else p
        except Exception:
            pass
    return Path(__file__).parent.parent / "data" / "presets"


def list_presets() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    d = _presets_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text()) or {}
            out.append({"name": data.get("name", p.stem), "description": data.get("description", ""), "locale": data.get("locale", "")})
        except Exception:
            continue
    return out


def load_preset(name: str) -> Dict:
    p = _presets_dir() / f"{name}.yaml"
    if not p.exists():
        raise ValueError(f"Unknown preset {name!r}. Available: {', '.join(x['name'] for x in list_presets()) or '(none)'}")
    data = yaml.safe_load(p.read_text()) or {}
    if not data.get("name"):
        data["name"] = name
    return data


def apply_preset(config: PIIConfig, preset_name: Optional[str]) -> PIIConfig:
    """Return a new PIIConfig with the preset merged in (non-destructive)."""
    if not preset_name:
        return config
    preset = load_preset(preset_name)
    columns = list(preset.get("columns", []) or [])
    extra = list(config.extra_pii_columns or [])
    for col in columns:
        pat = col.get("pattern") if isinstance(col, dict) else str(col)
        if pat and pat not in extra:
            extra.append(pat)
    locale = preset.get("locale") if not config.locale else config.locale
    return config.model_copy(update={"extra_pii_columns": extra, "locale": locale, "preset": preset.get("name", preset_name)})
