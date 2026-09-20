"""One-model creative lifecycle controller with injected side effects."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from synapse.core.creative_runner import run_creative_pipeline
from synapse.core.creative_sequence import (
    build_creative_sequence_plan,
    build_creative_sequence_receipt,
    record_creative_stage,
)


def run_creative_lifecycle(
    *, model_id: str, seed_hash: str, zone_plan: Sequence[str], load: Callable[[], bool], unload: Callable[[], bool], health: Callable[[], bool], request: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | str], timeout_seconds: int = 30,
    observe: Callable[[str, str], Mapping[str, Any] | None] | None = None,
    detailed_reactive: bool = False,
) -> dict[str, Any]:
    plan = build_creative_sequence_plan(model_id=model_id, zone_plan=zone_plan, timeout_seconds=timeout_seconds, detailed_reactive=detailed_reactive)
    events: list[str] = []
    observations: list[dict[str, Any]] = []
    def observe_stage(stage: str, phase: str) -> None:
        if observe is None:
            return
        try:
            value = observe(stage, phase)
            observations.append({"stage": stage, "phase": phase, "observation": dict(value or {})})
        except Exception as exc:  # noqa: BLE001 - observer failures are receipt data, not lifecycle failures
            observations.append({"stage": stage, "phase": phase, "observation_error": str(exc)[:240]})
    cleanup_on_exit = True
    try:
        plan = record_creative_stage(plan, "pre_unload", "RUNNING")
        observe_stage("pre_unload", "started")
        events.append("pre_unload_started")
        # Record the attempt before calling the external boundary. A false or
        # exceptional result is unknown/failed work, not permission to repeat it
        # from finally.
        cleanup_on_exit = False
        if not unload():
            plan = record_creative_stage(plan, "pre_unload", "FAILED", reason_code="PRE_UNLOAD_FAILED")
            return {"status": "FAILED", "error": "pre_unload_failed", "events": events, "sequence_receipt": build_creative_sequence_receipt(plan)}
        plan = record_creative_stage(plan, "pre_unload", "SUCCEEDED")
        cleanup_on_exit = True
        events.append("pre_unload_succeeded")
        plan = record_creative_stage(plan, "load", "RUNNING")
        observe_stage("load", "started")
        if not load():
            plan = record_creative_stage(plan, "load", "FAILED", reason_code="LOAD_FAILED")
            cleanup_on_exit = False
            unload()
            return {"status": "FAILED", "error": "load_failed", "events": events, "sequence_receipt": build_creative_sequence_receipt(plan)}
        plan = record_creative_stage(plan, "load", "SUCCEEDED")
        events.append("load_succeeded")
        plan = record_creative_stage(plan, "health_observation", "RUNNING")
        observe_stage("health_observation", "started")
        if not health():
            plan = record_creative_stage(plan, "health_observation", "FAILED", reason_code="HEALTH_FAILED")
            cleanup_on_exit = False
            unload()
            return {"status": "FAILED", "error": "health_failed", "events": events, "sequence_receipt": build_creative_sequence_receipt(plan)}
        plan = record_creative_stage(plan, "health_observation", "SUCCEEDED")
        events.append("health_succeeded")
        run = run_creative_pipeline(
            seed_hash=seed_hash,
            request=request,
            zone_plan=zone_plan,
            detailed_reactive=detailed_reactive,
        )
        observe_stage("map", "completed")
        map_ok = bool(run.get("stages") and run["stages"][0].get("status") == "VERIFIED")
        plan = record_creative_stage(plan, "map", "SUCCEEDED" if map_ok else "FAILED", detail=None if map_ok else str(run.get("error") or "map_verification_failed"), reason_code=None if map_ok else "MAP_VERIFICATION_FAILED")
        zone_results = {item.get("zone_type"): item for item in run.get("zones", [])}
        for zone in zone_plan:
            observe_stage(f"zone:{zone}", "started")
            zone_record = zone_results.get(zone, {})
            zone_ok = zone_record.get("status") == "VERIFIED"
            zone_state = "SUCCEEDED" if zone_ok else ("SKIPPED" if zone_record.get("status") == "SKIPPED" else "FAILED")
            plan = record_creative_stage(plan, f"zone:{zone}", zone_state, detail=None if zone_ok else str(zone_record.get("error") or zone_record.get("reason") or "zone_verification_failed"), reason_code=None if zone_ok else str(zone_record.get("error_code") or zone_record.get("error") or zone_record.get("reason") or "ZONE_VERIFICATION_FAILED"))
            observe_stage(f"zone:{zone}", "succeeded" if zone_ok else "failed")
        observe_stage("synthesis", "started")
        plan = record_creative_stage(plan, "synthesis", "SUCCEEDED" if run.get("status") == "READY_FOR_REVIEW" else "FAILED", detail=None if run.get("status") == "READY_FOR_REVIEW" else str(run.get("error") or "synthesis_blocked"), reason_code=None if run.get("status") == "READY_FOR_REVIEW" else str(run.get("error_code") or run.get("error") or "SYNTHESIS_BLOCKED"))
        observe_stage("synthesis", "succeeded" if run.get("status") == "READY_FOR_REVIEW" else "failed")
        plan = record_creative_stage(plan, "final_unload", "RUNNING")
        observe_stage("final_unload", "started")
        cleanup_on_exit = False
        cleanup_ok = unload()
        plan = record_creative_stage(plan, "final_unload", "SUCCEEDED" if cleanup_ok else "FAILED", reason_code=None if cleanup_ok else "FINAL_UNLOAD_FAILED")
        observe_stage("final_unload", "succeeded" if cleanup_ok else "failed")
        if not cleanup_ok:
            run["status"] = "CLEANUP_FAILED"
            run["execution_allowed"] = False
            run["cleanup_error"] = "final_unload_failed"
        run["events"] = events + ["final_unload_succeeded" if cleanup_ok else "final_unload_failed"]
        run["observations"] = observations
        run["sequence_receipt"] = build_creative_sequence_receipt(plan)
        return run
    finally:
        # The callback boundary owns cleanup policy; this controller never
        # mutates state or retries a provider operation.
        if cleanup_on_exit:
            unload()


__all__ = ["run_creative_lifecycle"]
