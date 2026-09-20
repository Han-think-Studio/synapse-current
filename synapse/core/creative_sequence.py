"""Deterministic lifecycle plan for one-model creative canaries.

This module plans and records stage boundaries only. It does not call a
provider, load a model, persist state, or apply generated artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

STAGE_STATES = {"NOT_STARTED", "RUNNING", "SUCCEEDED", "FAILED", "TIMEOUT", "NO_PROGRESS", "SKIPPED"}
RETRYABLE_ERROR_CODES = frozenset({"timeout", "transport", "malformed_response", "schema_error"})
_SENSITIVE_DETAIL = re.compile(r"(?i)(bearer\s+)[^\s,;]+|((?:api[_-]?key|token|password|secret|credential)\s*[:=]\s*)[^\s,;]+")


def _safe_detail(value: Any) -> str:
    """Keep failure diagnostics useful while removing credential-like values."""
    text = str(value)[:240]
    return _SENSITIVE_DETAIL.sub(lambda match: f"{match.group(1) or match.group(2)}[REDACTED]", text)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_creative_sequence_plan(*, model_id: str, zone_plan: Sequence[str], timeout_seconds: int = 30, detailed_reactive: bool = False) -> dict[str, Any]:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must be a non-empty string")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds < 1:
        raise ValueError("timeout_seconds must be a positive integer")
    zones = list(zone_plan)
    if not zones or any(not isinstance(zone, str) or not zone.strip() for zone in zones):
        raise ValueError("zone_plan must contain zone names")
    stages = ["pre_unload", "load", "health_observation", "map"]
    stages.extend(f"zone:{zone}" for zone in zones)
    stages.extend(["synthesis", "final_unload"])
    return {
        "schema_version": "creative.sequence.v1",
        "model_id": model_id.strip(),
        "requested_zone_plan": zones,
        "reactive_mode": "DETAILED" if detailed_reactive else "LEGACY",
        "timeout_policy": {"response_seconds": timeout_seconds, "load_seconds": timeout_seconds, "unload_seconds": timeout_seconds},
        "stages": [{"name": name, "state": "NOT_STARTED"} for name in stages],
        "canonical_mutation": False,
        "filesystem_mutation": False,
        "provider_task_execution": False,
        "execution_allowed": False,
    }


def record_creative_stage(plan: dict[str, Any], stage: str, state: str, *, detail: str | None = None, reason_code: str | None = None) -> dict[str, Any]:
    if state not in STAGE_STATES:
        raise ValueError("unsupported stage state")
    updated = dict(plan)
    stages = [dict(item) for item in plan.get("stages", [])]
    matches = [item for item in stages if item.get("name") == stage]
    if len(matches) != 1:
        raise ValueError("stage is not present exactly once")
    matches[0]["state"] = state
    if detail:
        matches[0]["detail"] = _safe_detail(detail)
    if reason_code:
        matches[0]["reason_code"] = _safe_detail(reason_code)
    updated["stages"] = stages
    return updated


def build_creative_sequence_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    stages = [dict(item) for item in plan.get("stages", [])]
    if not stages:
        raise ValueError("sequence plan has no stages")
    states = [item.get("state") for item in stages]
    if any(state not in STAGE_STATES for state in states):
        raise ValueError("sequence plan contains unsupported stage state")
    if any(state in {"TIMEOUT", "NO_PROGRESS"} for state in states):
        terminal = "TIMEOUT" if "TIMEOUT" in states else "NO_PROGRESS"
    elif any(state == "FAILED" for state in states):
        terminal = "FAILED"
    elif all(state == "SUCCEEDED" for state in states):
        terminal = "SUCCEEDED"
    elif all(state == "NOT_STARTED" for state in states):
        terminal = "NOT_STARTED"
    else:
        terminal = "RUNNING"
    failed_stage = next((item["name"] for item in stages if item.get("state") in {"FAILED", "TIMEOUT", "NO_PROGRESS"}), None)
    failed_item = next((item for item in stages if item.get("state") in {"FAILED", "TIMEOUT", "NO_PROGRESS"}), None)
    payload = {
        "schema_version": "creative.sequence.receipt.v1",
        "model_id": plan["model_id"],
        "requested_zone_plan": list(plan["requested_zone_plan"]),
        "reactive_mode": plan.get("reactive_mode", "LEGACY"),
        "stages": stages,
        "terminal_status": terminal,
        "completed_stage_count": sum(state == "SUCCEEDED" for state in states),
        "failed_stage": failed_stage,
        "failure_reason": (failed_item or {}).get("reason_code") if failed_item else None,
        "failure_detail": (failed_item or {}).get("detail") if failed_item else None,
        "timeout_policy": dict(plan["timeout_policy"]),
        "canonical_mutation": False,
        "filesystem_mutation": False,
        "provider_task_execution": False,
        "execution_allowed": False,
    }
    payload["source_plan_hash"] = _hash({key: value for key, value in plan.items() if key != "receipt_hash"})
    payload["receipt_hash"] = _hash(payload)
    return payload


def classify_stage_observation(*, elapsed_seconds: float, timeout_seconds: float, process_alive: bool | None, log_changed: bool, response_received: bool) -> str:
    """Classify a read-only observation without deciding provider policy."""

    if response_received:
        return "SUCCEEDED"
    if elapsed_seconds < 0 or timeout_seconds <= 0:
        raise ValueError("elapsed_seconds and timeout_seconds must be valid")
    if elapsed_seconds >= timeout_seconds:
        if process_alive is False and not log_changed:
            return "NO_PROGRESS"
        return "TIMEOUT"
    if process_alive is False and not log_changed:
        return "NO_PROGRESS"
    return "RUNNING"


def is_retryable_stage_error(error_code: str) -> bool:
    """Return whether an error may be retried by an external sequence owner."""
    return isinstance(error_code, str) and error_code.strip().lower() in RETRYABLE_ERROR_CODES


def cleanup_action_for_observation(status: str, *, process_alive: bool | None) -> str:
    """Select a required cleanup action after a bounded provider observation."""
    normalized = status.strip().upper() if isinstance(status, str) else ""
    if normalized in {"TIMEOUT", "NO_PROGRESS"} or process_alive is True:
        return "FORCE_UNLOAD_AND_VERIFY"
    if normalized in {"FAILED", "SUCCEEDED"}:
        return "UNLOAD_AND_VERIFY"
    return "NO_ACTION"


__all__ = ["RETRYABLE_ERROR_CODES", "STAGE_STATES", "build_creative_sequence_plan", "build_creative_sequence_receipt", "classify_stage_observation", "cleanup_action_for_observation", "is_retryable_stage_error", "record_creative_stage"]
