"""Bounded, provider-agnostic creative pipeline runner.

The caller injects one already-authorized request function. This module does
not load models, manage provider state, persist output, or apply artifacts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from synapse.core.creative_consistency import validate_cross_zone_consistency
from synapse.core.creative_contracts import (
    PRIOR_ID_BYTES_LIMIT,
    PRIOR_ID_LIMIT,
    build_creative_frame_contract,
    validate_precontract_materialization,
)
from synapse.core.creative_quality import (
    _reactive_contract_status,
    missing_required_output_content,
    missing_required_validation_cases,
)
from synapse.core.creative_runtime_bridge import validate_provider_creative_response
from synapse.core.creative_synthesis import build_creative_synthesis_receipt
from synapse.core.idea_session import canonical_hash


class CreativeResponseError(ValueError):
    """Raised when a provider response cannot be reduced to one JSON object."""


def _wire_content(value: Any) -> Any:
    """Return a detached JSON-shaped copy of validated creative content."""

    if isinstance(value, Mapping):
        return {key: _wire_content(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire_content(item) for item in value]
    return value


def _response_object(value: Any) -> Mapping[str, Any]:
    """Parse provider text without requiring a provider-specific response format."""

    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        raise CreativeResponseError("MALFORMED_RESPONSE: empty or non-object response")
    text = value.strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.insert(0, fenced.group(1))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            return parsed
    raise CreativeResponseError("MALFORMED_RESPONSE: expected one JSON object")


def _verified_zone_reference(zone: Mapping[str, Any], *, global_map: Mapping[str, Any]) -> dict[str, Any]:
    """Build the small, immutable handoff a later zone may rely on.

    The full prior proposal is deliberately not copied into the next prompt.
    Its identity, map binding, source references, and canonical content hash
    are enough for a provider to declare what it used, while keeping the
    runner bounded and avoiding a second state store.
    """

    content = {
        "schema_version": zone["schema_version"],
        "zone_id": zone["zone_id"],
        "zone_type": zone["zone_type"],
        "map_id": zone["map_id"],
        "map_hash": zone["map_hash"],
        "source_refs": list(zone["source_refs"]),
        "outputs": dict(zone["outputs"]),
        "validation": {key: list(values) for key, values in zone["validation"].items()},
        # Reactive contracts are part of the verified handoff.  Omitting them
        # would let a guard/effect change reuse an old downstream reference.
        "reactive_contract": dict(zone["reactive_contract"]) if isinstance(zone.get("reactive_contract"), Mapping) else None,
        "status": zone["status"],
    }
    return {
        "zone_id": zone["zone_id"],
        "zone_type": zone["zone_type"],
        "map_id": zone["map_id"],
        "map_hash": zone["map_hash"],
        "content_hash": canonical_hash(content),
        "reactive_contract_hash": canonical_hash(content["reactive_contract"]),
        "source_refs": list(zone["source_refs"]),
        "materialized_ids": [
            dict(item)
            for item in validate_precontract_materialization(
                zone["zone_type"], zone["outputs"], global_map=global_map
            )
        ],
    }


def _zone_fill_contract(
    zone_type: str, *, global_map: Mapping[str, Any], frame_contract: Mapping[str, Any]
) -> dict[str, Any]:
    bound_precontract = global_map["precontract"]
    dependency_by_zone = {
        item["zone_type"]: list(item["requires"])
        for item in bound_precontract["generation_dependencies"]
    }
    required_fields = frame_contract["required_output_fields"][zone_type]
    return {
        "zone_type": zone_type,
        "required_output_fields": list(required_fields),
        "required_validation_cases": list(frame_contract["required_validation_cases"]),
        "id_slots": [item for item in bound_precontract["id_slots"] if item["zone_type"] == zone_type],
        "required_links": [item for item in bound_precontract["required_links"] if item["source_zone"] == zone_type],
        "requires_prior_zones": dependency_by_zone[zone_type],
        "precontract_hash": global_map["precontract_hash"],
    }


def _prior_id_projection(
    zone_contract: Mapping[str, Any], prior_zone_refs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    required_zones = set(zone_contract["requires_prior_zones"])
    projected = [
        {"zone_type": reference["zone_type"], "id_slots": list(reference["materialized_ids"])}
        for reference in prior_zone_refs
        if reference["zone_type"] in required_zones and reference.get("materialized_ids")
    ]
    id_count = sum(len(item["id_slots"]) for item in projected)
    encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if id_count > PRIOR_ID_LIMIT or len(encoded) > PRIOR_ID_BYTES_LIMIT:
        raise CreativeResponseError("PRECONTRACT_PRIOR_ID_BUDGET_EXCEEDED")
    present = {item["zone_type"] for item in projected}
    if not required_zones.issubset(present):
        raise CreativeResponseError("PRECONTRACT_PRIOR_IDS_NOT_MATERIALIZED")
    return projected


def run_creative_pipeline(
    *, seed_hash: str, request: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | str],
    zone_plan: Sequence[str], context: Mapping[str, Any] | None = None,
    detailed_reactive: bool = False,
) -> dict[str, Any]:
    """Run map then zones in order and return a memory-only run receipt.

    ``detailed_reactive`` is an explicit, opt-in contract mode.  It only
    tightens the response boundary: every verified zone must carry a complete
    reactive contract before quality/synthesis can run.  The default remains
    compatible with the legacy creative envelope and does not enable broader
    provider execution or alter context/state handling.
    """

    stages: list[dict[str, Any]] = []
    context = dict(context or {})
    try:
        frame_contract = build_creative_frame_contract(zone_plan)
    except Exception as exc:  # noqa: BLE001 - reject caller plan errors before provider dispatch
        return {"schema_version": "creative.run.v1", "seed_hash": seed_hash, "status": "BLOCKED", "stages": [{"stage": "global_map", "status": "REJECTED", "error": str(exc)}], "canonical_mutation": False, "filesystem_mutation": False, "execution_allowed": False}
    try:
        global_map = validate_provider_creative_response(
            stage="global_map",
            response=request(
                "global_map",
                {
                    **context,
                    "seed_hash": seed_hash,
                    "requested_zone_plan": list(zone_plan),
                    "frame_contract": frame_contract,
                    # The explicit runner mode is authoritative; caller
                    # context must not silently override it.
                    "detailed_reactive": detailed_reactive,
                },
            ),
            seed_hash=seed_hash,
        )
    except Exception as exc:  # noqa: BLE001 - normalize any untrusted provider failure into a blocked receipt
        return {"schema_version": "creative.run.v1", "seed_hash": seed_hash, "status": "BLOCKED", "stages": [{"stage": "global_map", "status": "REJECTED", "error": str(exc)}], "canonical_mutation": False, "filesystem_mutation": False, "execution_allowed": False}
    if global_map["status"] == "REJECTED":
        return {
            "schema_version": "creative.run.v1",
            "seed_hash": seed_hash,
            "status": "BLOCKED",
            "stages": [{"stage": "global_map", "status": "REJECTED", "error": "GLOBAL_MAP_REJECTED"}],
            "global_map": _wire_content(global_map),
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "execution_allowed": False,
        }
    if tuple(global_map["zone_plan"]) != tuple(zone_plan):
        return {
            "schema_version": "creative.run.v1",
            "seed_hash": seed_hash,
            "status": "BLOCKED",
            "stages": [{"stage": "global_map", "status": "REJECTED", "error": "ZONE_PLAN_MISMATCH"}],
            "global_map": _wire_content(global_map),
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "execution_allowed": False,
        }
    stages.append({"stage": "global_map", "status": "VERIFIED", "map_id": global_map["map_id"], "map_hash": global_map["map_hash"], "precontract_hash": global_map["precontract_hash"]})
    # Contract validators intentionally accept wire JSON lists. Keep the
    # normalized tuple out of the next boundary while preserving its values.
    bound_map = dict(global_map)
    bound_map["zone_plan"] = list(global_map["zone_plan"])
    bound_map["invariants"] = list(global_map["invariants"])
    zones: list[Mapping[str, Any]] = []
    zone_records: list[dict[str, Any]] = []
    prior_zone_refs: list[dict[str, Any]] = []
    for zone_type in zone_plan:
        try:
            zone_contract = _zone_fill_contract(
                zone_type, global_map=global_map, frame_contract=frame_contract
            )
            prior_ids = _prior_id_projection(zone_contract, prior_zone_refs)
            prompt_refs = [
                {key: value for key, value in reference.items() if key != "materialized_ids"}
                for reference in prior_zone_refs
            ]
            zone_payload = {
                **context,
                "zone_type": zone_type,
                "global_map": bound_map,
                "zone_contract": zone_contract,
                "prior_zone_refs": prompt_refs,
                "prior_ids": prior_ids,
                # The runner owns this opt-in boundary.  Put it after caller
                # context so a stale or conflicting context value cannot
                # silently enable/disable the zone contract.
                "detailed_reactive": detailed_reactive,
            }
            zone = validate_provider_creative_response(
                stage="zone",
                response=request("zone", zone_payload),
                seed_hash=seed_hash,
                global_map=bound_map,
            )
            if zone["zone_type"] != zone_type:
                raise CreativeResponseError(
                    f"CREATIVE_ZONE_TYPE_MISMATCH: expected {zone_type}, got {zone['zone_type']}"
                )
            if zone["status"] == "REJECTED":
                raise CreativeResponseError("CREATIVE_ZONE_REJECTED: provider returned a rejected zone")
            missing_outputs = missing_required_output_content(
                zone["zone_type"], zone["outputs"], require_invariant_refs=True
            )
            if missing_outputs:
                raise CreativeResponseError(
                    "MINIMUM_CONTENT_MISSING: " + ",".join(missing_outputs)
                )
            missing_validation_cases = missing_required_validation_cases(zone["zone_type"], zone["validation"])
            if missing_validation_cases:
                raise CreativeResponseError(
                    "MINIMUM_VALIDATION_MISSING: " + ",".join(missing_validation_cases)
                )
            validate_precontract_materialization(
                zone_type,
                zone["outputs"],
                global_map=bound_map,
                verified_prior_zones=zones,
            )
            if detailed_reactive:
                status, errors = _reactive_contract_status(zone, detailed_reactive=True)
                if status != "VERIFIED":
                    raise CreativeResponseError(
                        "DETAILED_REACTIVE_CONTRACT_BLOCKED: " + ",".join(errors or [zone_type])
                    )
            zones.append(zone)
            reference = _verified_zone_reference(zone, global_map=bound_map)
            prior_zone_refs.append(reference)
            zone_records.append({
                "zone_type": zone_type,
                "status": "VERIFIED",
                "zone_id": zone["zone_id"],
                "content_hash": reference["content_hash"],
                "content": _wire_content(zone),
            })
        except Exception as exc:  # noqa: BLE001 - plugin/validator failures reject this zone and stop retries
            zone_records.append({"zone_type": zone_type, "status": "REJECTED", "retryable": not str(exc).startswith("PRECONTRACT_"), "error": str(exc)})
            # Safe default: do not spend quota or create dependent output
            # after a zone contract failure. The partial receipt preserves
            # the failure and lets an external orchestrator decide retry.
            for remaining in zone_plan[len(zone_records):]:
                zone_records.append({"zone_type": remaining, "status": "SKIPPED", "reason": "prior_zone_failed"})
            break
    if len(zones) != len(tuple(zone_plan)) or tuple(zone_plan) != tuple(global_map["zone_plan"]):
        return {"schema_version": "creative.run.v1", "seed_hash": seed_hash, "map_id": global_map["map_id"], "map_hash": global_map["map_hash"], "precontract_hash": global_map["precontract_hash"], "global_map": _wire_content(bound_map), "status": "BLOCKED", "stages": stages, "zones": zone_records, "canonical_mutation": False, "filesystem_mutation": False, "execution_allowed": False}
    synthesis_zones = []
    for zone in zones:
        wire_zone = dict(zone)
        wire_zone["source_refs"] = list(zone["source_refs"])
        wire_zone["validation"] = {key: list(values) for key, values in zone["validation"].items()}
        synthesis_zones.append(wire_zone)
    try:
        validate_cross_zone_consistency(bound_map, synthesis_zones)
        synthesis = build_creative_synthesis_receipt(bound_map, synthesis_zones, detailed_reactive=detailed_reactive)
    except Exception as exc:  # noqa: BLE001 - synthesis boundary returns a blocked receipt on any contract failure
        return {"schema_version": "creative.run.v1", "seed_hash": seed_hash, "map_id": global_map["map_id"], "map_hash": global_map["map_hash"], "precontract_hash": global_map["precontract_hash"], "global_map": _wire_content(bound_map), "status": "BLOCKED", "stages": stages, "zones": zone_records, "error": str(exc), "canonical_mutation": False, "filesystem_mutation": False, "execution_allowed": False}
    return {"schema_version": "creative.run.v1", "seed_hash": seed_hash, "map_id": global_map["map_id"], "map_hash": global_map["map_hash"], "precontract_hash": global_map["precontract_hash"], "global_map": _wire_content(bound_map), "status": synthesis["status"], "stages": stages, "zones": zone_records, "synthesis": synthesis, "detailed_reactive": detailed_reactive, "canonical_mutation": False, "filesystem_mutation": False, "execution_allowed": False}


__all__ = ["CreativeResponseError", "run_creative_pipeline"]
