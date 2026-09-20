"""Read-only implementation progress projection derived from ``spec/``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _gate_count(raw: dict[str, Any]) -> int:
    """Count gate criteria whether they are inline or archived.

    A closed phase keeps ``gate_count`` in ``spec/phases.yaml`` while its full gate
    text lives in ``spec/phases/archive/``; the current phase still carries ``gate``
    inline. Both must project the same number.
    """
    gate = raw.get("gate")
    if isinstance(gate, list):
        return len(gate)
    count = raw.get("gate_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return 0


def build_progress(spec_path: str | Path | None = None) -> dict[str, Any]:
    """Project phase completion without relying on model or time estimates."""
    path = Path(spec_path) if spec_path is not None else Path(__file__).resolve().parents[2] / "spec" / "phases.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"phase progress를 읽을 수 없습니다: {path}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("phases"), list):
        raise TypeError("phases.yaml 구조가 잘못되었습니다.")

    current = int(document.get("current_phase", -1))
    phases: list[dict[str, Any]] = []
    for raw in document["phases"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("number"), int):
            raise TypeError("phase 항목이 잘못되었습니다.")
        status = str(raw.get("status", "")).strip()
        if status == "completed":
            percent, state = 100, "VERIFIED"
        elif status == "in_progress":
            percent, state = 50, "IN_PROGRESS"
        elif status.startswith("blocked"):
            percent, state = 0, "BLOCKED"
        else:
            percent, state = 0, "NOT_STARTED"
        phases.append(
            {
                "number": raw["number"],
                "name": str(raw.get("name", "")),
                "status": status,
                "state": state,
                "percent": percent,
                "gate_count": _gate_count(raw),
                "outputs": list(raw.get("outputs", ())) if isinstance(raw.get("outputs", ()), list) else [],
            }
        )
    phases.sort(key=lambda phase: phase["number"])
    overall = round(sum(phase["percent"] for phase in phases) / len(phases)) if phases else 0
    return {"current_phase": current, "overall_percent": overall, "phases": phases}


__all__ = ["build_progress"]
