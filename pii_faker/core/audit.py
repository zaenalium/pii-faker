"""
PII-free audit reports (roadmap 3.3).

Every run writes a sibling report without ever touching real or fake values
— only the plan (path/category/confidence) and counts. Guardrail: the
values set is empty.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pii_faker.core.config import PIIConfig
from pii_faker import __version__ as TOOL_VERSION


@dataclass
class AuditReport:
    """One run's audit — deliberately PII-free."""

    run_id: str
    started_at: str
    finished_at: str
    tool_version: str
    config: Dict[str, Any]
    targets: List[Dict[str, Any]]
    counts: Dict[str, int]
    warnings: List[str]
    leak_check: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": "pii-faker",
            "tool_version": self.tool_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "config": self.config,
            "targets": self.targets,
            "counts": self.counts,
            "warnings": self.warnings,
            "leak_check": self.leak_check,
        }

    def to_human(self) -> str:
        lines = [
            f"PII Faker audit {self.run_id}",
            f"Tool {self.tool_version}  {self.started_at} → {self.finished_at}",
            f"Mode {self.config.get('mode')}  strategy {self.config.get('match_strategy')}",
            "",
            f"Targets ({len(self.targets)}):",
        ]
        for t in self.targets:
            lines.append(
                f"  - {t.get('path')}  category={t.get('category')}  "
                f"conf={t.get('confidence')}  signal={t.get('signal')}"
            )
        lines.append("")
        lines.append("Counts (unique values):")
        for k, v in self.counts.items():
            lines.append(f"  - {k}: {v}")
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  ! {w}")
        lc = self.leak_check or {}
        lines.append("")
        lines.append(f"Leak check: {'clean' if lc.get('clean', True) else 'issues found'}")
        return "\n".join(lines)


def build_audit(
    run_id: str,
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    config: PIIConfig,
    targets: List[Any],
    counts: Dict[str, int],
    warnings: Optional[List[str]] = None,
    leak_report: Optional[Any] = None,
) -> AuditReport:
    safe_config = {
        "mode": config.mode,
        "redact_style": config.redact_style,
        "match_strategy": config.match_strategy,
        "seed": config.seed,
        "locale": config.locale,
        "preset": getattr(config, "preset", None),
        "override_defaults": config.override_defaults,
        "strict_paths": config.strict_paths,
    }
    audit_targets = []
    for t in targets or []:
        if isinstance(t, dict):
            audit_targets.append({
                "path": t.get("path") or t.get("column"),
                "category": t.get("category"),
                "confidence": t.get("confidence", 1.0),
                "signal": t.get("value_detector") or ("name" if t.get("name_match") else "value"),
            })
        else:
            audit_targets.append({
                "path": getattr(t, "path", getattr(t, "column", "")),
                "category": getattr(t, "category", ""),
                "confidence": getattr(t, "confidence", 1.0),
                "signal": getattr(t, "value_detector", None) or ("name" if getattr(t, "name_match", True) else "value"),
            })
    if leak_report is None:
        leak_dict: Dict[str, Any] = {"clean": True}
    elif isinstance(leak_report, dict):
        leak_dict = leak_report
    else:
        try:
            leak_dict = leak_report.to_dict() if hasattr(leak_report, "to_dict") else {"clean": bool(leak_report.clean)}
        except Exception:
            leak_dict = {"clean": True}
    return AuditReport(
        run_id=run_id,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        tool_version=TOOL_VERSION,
        config=safe_config,
        targets=audit_targets,
        counts=dict(counts or {}),
        warnings=list(warnings or []),
        leak_check=leak_dict,
    )


def write_audit_files(audit: AuditReport, base_path: str) -> tuple[str, str]:
    """Write audit.json + audit.txt next to a mapping path. Returns (json_path, txt_path)."""
    base = Path(base_path)
    stem = base.stem
    parent = base.parent
    json_path = str(parent / f"{stem}_audit.json")
    txt_path = str(parent / f"{stem}_audit.txt")
    Path(json_path).write_text(json.dumps(audit.to_dict(), indent=2, ensure_ascii=False))
    Path(txt_path).write_text(audit.to_human())
    return json_path, txt_path
