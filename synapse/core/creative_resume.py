"""Proposal-only validation for resuming creative stage receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def evaluate_creative_resume(*, receipt: Mapping[str, Any], run_id: str, task_id: str, seed_hash: str, map_hash: str, zone_plan: Sequence[str]) -> dict[str, Any]:
    """Return a non-authoritative resume decision; never re-executes a stage."""
    errors: list[str] = []
    if receipt.get("run_id") not in {None, run_id}:
        errors.append("run_id_mismatch")
    if receipt.get("task_id") not in {None, task_id}:
        errors.append("task_id_mismatch")
    if receipt.get("seed_hash") not in {None, seed_hash}:
        errors.append("seed_hash_mismatch")
    if receipt.get("map_hash") not in {None, map_hash}:
        errors.append("map_hash_mismatch")
    if receipt.get("schema_version") not in {"creative.sequence.receipt.v1", "creative.run.v1"}:
        errors.append("unsupported_receipt_schema")
    supplied_hash = receipt.get("receipt_hash")
    if supplied_hash is not None:
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash"}
        encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        expected_hash = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        if supplied_hash != expected_hash:
            errors.append("receipt_hash_mismatch")
    stages = receipt.get("stages", [])
    if not isinstance(stages, list):
        errors.append("stages_invalid")
        stages = []
    failed = any(item.get("status", item.get("state")) in {"FAILED", "TIMEOUT", "NO_PROGRESS"} for item in stages if isinstance(item, Mapping))
    if failed:
        errors.append("failed_stage_present")
    expected = tuple(zone_plan)
    seen_zones = tuple(item.get("zone_type") for item in receipt.get("zones", []) if isinstance(item, Mapping))
    if seen_zones and seen_zones != expected[:len(seen_zones)]:
        errors.append("zone_plan_mismatch")
    completed = {
        "map" if item.get("stage", item.get("name")) == "global_map" else item.get("stage", item.get("name"))
        for item in stages
        if isinstance(item, Mapping) and item.get("status", item.get("state")) in {"SUCCEEDED", "VERIFIED"}
    }
    completed.update(
        f"zone:{item.get('zone_type')}" for item in receipt.get("zones", [])
        if isinstance(item, Mapping) and item.get("status") == "VERIFIED" and item.get("zone_type")
    )
    next_stage = next((stage for stage in ("map", *[f"zone:{zone}" for zone in expected], "synthesis") if stage not in completed), None)
    return {"schema_version": "creative.resume.decision.v1", "status": "BLOCKED" if errors else "PROPOSED", "next_stage": next_stage, "errors": sorted(set(errors)), "execution_allowed": False, "canonical_mutation": False, "filesystem_mutation": False}


__all__ = ["evaluate_creative_resume"]
