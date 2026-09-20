"""Deterministic, proposal-only quality gate for creative map synthesis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from synapse.core.creative_consistency import validate_cross_zone_consistency
from synapse.core.creative_contracts import (
    DETAILED_REACTIVE_CONTRACT_VERSION,
    MINIMUM_CONTENT_PLACEHOLDERS,
    ZONE_OUTPUT_FIELDS,
    CreativeContractError,
    has_minimum_creative_content,
    inspect_precontract_materialization,
    validate_global_map_envelope,
    validate_zone_envelope,
)
from synapse.core.creative_typed_outputs import validate_rich_zone_outputs
from synapse.core.idea_session import canonical_hash

QUALITY_WEIGHTS = {
    "map_traceability": 15,
    "causal_coverage": 20,
    "agency_and_world": 15,
    "foreshadowing_and_payoff": 15,
    "scene_executability": 15,
    "script_data_reactivity": 10,
    "ending_completeness": 10,
}
REACTIVE_CONTRACT_FIELDS = ("stable_id", "input_refs", "event_or_trigger", "guard_or_precondition", "effect_or_state_delta", "downstream_impact_refs", "normal_boundary_failure_cases")
REACTIVE_CONTRACT_VERSION = DETAILED_REACTIVE_CONTRACT_VERSION
_REQUIRED_VALIDATION_CASES = ("normal", "boundary", "failure")


def _has_semantic_content(value: Any) -> bool:
    """Reject output envelopes whose values are only empty placeholders."""

    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value) and any(_has_semantic_content(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bool(value) and any(_has_semantic_content(item) for item in value)
    return value is not None


def missing_required_validation_cases(zone_type: str, validation: Mapping[str, Any]) -> list[str]:
    """List required validation case arrays without minimum usable content."""

    return [
        f"{zone_type}.{case_type}"
        for case_type in _REQUIRED_VALIDATION_CASES
        if not has_minimum_creative_content(validation.get(case_type))
    ]


def missing_required_output_content(
    zone_type: str, outputs: Mapping[str, Any], *, require_invariant_refs: bool = False
) -> list[str]:
    """List required zone outputs that lack minimum substantive content."""

    required_fields = ZONE_OUTPUT_FIELDS.get(zone_type, ())
    if require_invariant_refs:
        required_fields = (*required_fields, "invariant_refs")
    return [
        f"{zone_type}.{key}"
        for key in required_fields
        if not has_minimum_creative_content(outputs.get(key), output_field=key)
    ]


def _has_meaningful_recovery(value: Any) -> bool:
    """Require an explicit recovery/handling value for reactive test cases."""

    if isinstance(value, str):
        return bool(value.strip()) and value.strip().casefold() not in MINIMUM_CONTENT_PLACEHOLDERS
    if isinstance(value, Mapping):
        return bool(value) and any(_has_meaningful_recovery(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bool(value) and any(_has_meaningful_recovery(item) for item in value)
    return value is not None


def _reactive_case_entries(value: Any) -> list[tuple[str, Any]]:
    """Normalize explicit normal/boundary/failure cases without inferring prose."""

    if isinstance(value, Mapping):
        return [(str(key).strip().casefold(), item) for key, item in value.items()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        entries: list[tuple[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                kind = item.get("kind", item.get("case"))
                if isinstance(kind, str):
                    entries.append((kind.strip().casefold(), item))
        return entries
    return []


def _reactive_case_errors(value: Any) -> list[str]:
    """Return strict case errors; every case needs expected and recovery data."""

    entries = _reactive_case_entries(value)
    required = {"normal", "boundary", "failure"}
    errors: list[str] = []
    kinds = {kind for kind, _ in entries}
    errors.extend(f"missing_case:{kind}" for kind in sorted(required - kinds))
    for kind, case in entries:
        if not isinstance(case, Mapping):
            errors.append(f"case_without_details:{kind}")
            continue
        expected = case.get("expected", case.get("result", case.get("outcome")))
        recovery = case.get("recovery", case.get("on_failure", case.get("remediation", case.get("handling"))))
        if not _has_meaningful_recovery(expected):
            errors.append(f"missing_expected:{kind}")
        if not _has_meaningful_recovery(recovery):
            errors.append(f"missing_recovery:{kind}")
    return errors


def _zone_content_hash(zone: Mapping[str, Any]) -> str:
    payload = {key: zone[key] for key in ("zone_id", "zone_type", "map_id", "map_hash", "source_refs", "outputs", "validation", "reactive_contract", "status")}
    return canonical_hash(payload)


def _structure_validation_errors(
    global_map: Mapping[str, Any],
    bound: Mapping[str, Any],
    zones: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Apply the same rich-output and cross-zone checks used before generation synthesis."""

    errors: list[str] = []
    by_type = {zone["zone_type"]: zone for zone in zones}
    for zone in zones:
        try:
            validate_rich_zone_outputs(zone["zone_type"], zone["outputs"])
        except CreativeContractError as exc:
            errors.append(f"typed_output:{zone['zone_type']}:{exc}")
    ordered = []
    for zone_type in bound["zone_plan"]:
        zone = by_type[zone_type]
        ordered.append({
            **zone,
            "source_refs": list(zone["source_refs"]),
            "validation": {key: list(values) for key, values in zone["validation"].items()},
        })
    try:
        validate_cross_zone_consistency(global_map, ordered)
    except (CreativeContractError, TypeError) as exc:
        errors.append(f"cross_zone:{exc}")
    return sorted(errors)


def _precontract_fill_errors(
    global_map: Mapping[str, Any],
    bound: Mapping[str, Any],
    zones: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Report slot and required-link fill errors independently of other checks."""

    if bound["precontract"] is None:
        return []
    errors = []
    for zone in zones:
        try:
            _, blockers = inspect_precontract_materialization(
                zone["zone_type"], zone["outputs"], global_map=global_map
            )
            errors.extend(f"{zone['zone_type']}:{blocker}" for blocker in blockers)
        except (CreativeContractError, TypeError) as exc:
            errors.append(f"{zone['zone_type']}:{exc}")
    return sorted(errors)


def _reactive_contract_status(zone: Mapping[str, Any], *, detailed_reactive: bool = False) -> tuple[str, list[str]]:
    contract = zone.get("reactive_contract")
    if contract is None:
        return "NOT_SUPPLIED", []
    missing = [key for key in REACTIVE_CONTRACT_FIELDS if key not in contract or not _has_semantic_content(contract[key])]
    errors = [f"{zone['zone_type']}:missing_reactive:{key}" for key in missing]
    if detailed_reactive:
        version = contract.get("contract_version")
        if version != DETAILED_REACTIVE_CONTRACT_VERSION:
            kind = "missing" if not _has_semantic_content(version) else "invalid"
            errors.append(f"{zone['zone_type']}:{kind}_reactive:contract_version")
    # Older callers may omit the version; preserve that compatibility while
    # rejecting an explicitly supplied unknown version. New runtime requests
    # always emit the version and therefore receive strict binding.
    if contract.get("contract_version") is not None and contract.get("contract_version") != REACTIVE_CONTRACT_VERSION:
        errors.append(f"{zone['zone_type']}:unsupported_reactive_contract_version")
    if "normal_boundary_failure_cases" not in missing:
        errors.extend(
            f"{zone['zone_type']}:invalid_reactive_case:{item}"
            for item in _reactive_case_errors(contract["normal_boundary_failure_cases"])
        )
    return ("VERIFIED", []) if not errors else ("BLOCKED", errors)


def evaluate_creative_quality(
    global_map: Mapping[str, Any], zones: Sequence[Mapping[str, Any]], *, threshold: int = 80,
    detailed_reactive: bool = False,
) -> dict[str, Any]:
    """Return a stable score and gate decision; never mutates or applies output.

    ``detailed_reactive`` is an explicit opt-in for callers that want the
    quality gate itself to require a verified reactive contract on every
    zone.  The default remains the legacy-compatible behavior.
    """

    if not isinstance(threshold, int) or not 0 <= threshold <= 100:
        raise ValueError("threshold must be an integer from 0 to 100")
    bound = validate_global_map_envelope(global_map)
    validated = [validate_zone_envelope(zone, global_map=global_map) for zone in zones]
    zone_types = {zone["zone_type"] for zone in validated}
    planned = set(bound["zone_plan"])
    if zone_types != planned or len(validated) != len(planned):
        raise ValueError("quality synthesis requires exactly one result per planned zone")

    structure_validation_errors = _structure_validation_errors(bound=bound, global_map=global_map, zones=validated)
    structure_valid = not structure_validation_errors
    precontract_fill_errors = _precontract_fill_errors(global_map, bound, validated)

    missing_validation_cases = sorted(
        path
        for zone in validated
        for path in missing_required_validation_cases(zone["zone_type"], zone["validation"])
    )
    complete_cases = not missing_validation_cases
    traceable = all(zone["map_id"] == bound["map_id"] and zone["map_hash"] == bound["map_hash"] for zone in validated)
    missing_outputs = sorted(
        path
        for zone in validated
        for path in missing_required_output_content(
            zone["zone_type"],
            zone["outputs"],
            require_invariant_refs=bound["precontract"] is not None,
        )
    )
    has_outputs = not missing_outputs
    reactive_zones = [zone for zone in validated if zone["zone_type"] == "script_data_reactive"]
    reactive_complete = all(
        all(
            _has_semantic_content(zone["outputs"].get(key))
            for key in ("data_entities", "state_transitions", "triggers", "handlers", "interfaces", "validation_cases")
        )
        for zone in reactive_zones
    )
    reactive_status = {}
    reactive_versions = {}
    reactive_errors = []
    for zone in validated:
        status, errors = _reactive_contract_status(zone, detailed_reactive=detailed_reactive)
        contract = zone.get("reactive_contract")
        reactive_versions[zone["zone_type"]] = contract.get("contract_version") if isinstance(contract, Mapping) else None
        if detailed_reactive and status == "NOT_SUPPLIED":
            errors = [f"{zone['zone_type']}:missing_reactive_contract"]
            status = "BLOCKED"
        reactive_status[zone["zone_type"]] = status
        reactive_errors.extend(errors)
    score = dict(QUALITY_WEIGHTS)
    if not traceable:
        score["map_traceability"] = 0
    if not complete_cases:
        score["scene_executability"] = 0
    if "script_data_reactive" not in zone_types:
        score["script_data_reactivity"] = 0
    if not has_outputs:
        score["causal_coverage"] = 0
    if not reactive_complete:
        score["script_data_reactivity"] = 0
    invariant_names = set(bound["invariants"])
    covered_invariants: set[str] = set()
    conflicts: list[str] = []
    rejected_zones: list[str] = []
    passed_zones: list[str] = []
    for zone in validated:
        (rejected_zones if zone["status"] == "REJECTED" else passed_zones).append(zone["zone_id"])
        refs = zone["outputs"].get("invariant_refs", [])
        if isinstance(refs, list):
            covered_invariants.update(item for item in refs if isinstance(item, str))
        raw_conflicts = zone["outputs"].get("conflicts", [])
        if isinstance(raw_conflicts, list):
            conflicts.extend(item for item in raw_conflicts if isinstance(item, str) and item.strip())
    invariant_coverage = invariant_names <= covered_invariants
    unknown_invariants = covered_invariants - invariant_names
    if not invariant_coverage:
        score["map_traceability"] = 0
    total = sum(score.values())
    evidence = {"zone_count": len(validated), "complete_cases": complete_cases,
                "traceable": traceable, "outputs_present": has_outputs,
                "global_map_status": bound["status"],
                "global_map_rejected": bound["status"] == "REJECTED",
                "precontract_status": "VERIFIED" if bound["precontract"] is not None else "MISSING_LEGACY",
                "precontract_fill_status": (
                    "NOT_ASSESSED"
                    if bound["precontract"] is None
                    else "BLOCKED" if precontract_fill_errors else "VERIFIED"
                ),
                "precontract_fill_errors": precontract_fill_errors,
                "precontract_hash": bound["precontract_hash"],
                "structure_valid": structure_valid,
                "structure_validation_errors": structure_validation_errors,
                "reactive_complete": reactive_complete,
                "covered_invariants": sorted(covered_invariants & invariant_names),
                "uncovered_invariants": sorted(invariant_names - covered_invariants),
                "unknown_invariants": sorted(unknown_invariants),
                "conflicts": sorted(set(conflicts)),
                "passed_zones": sorted(passed_zones),
                "retry_zones": sorted(rejected_zones),
                "zone_content_hashes": {zone["zone_type"]: _zone_content_hash(zone) for zone in sorted(validated, key=lambda item: item["zone_type"])},
                "reactive_contract_status": reactive_status,
                "reactive_contract_versions": reactive_versions,
                "reactive_contract_errors": sorted(reactive_errors),
                "detailed_reactive": detailed_reactive}
    if missing_outputs:
        # Keep valid receipt payloads and their hashes byte-for-byte stable;
        # add field-level diagnosis only on the newly blocked path.
        evidence["missing_outputs"] = missing_outputs
    if missing_validation_cases:
        evidence["missing_validation_cases"] = missing_validation_cases
    hard_block = not (
        bound["status"] != "REJECTED"
        and bound["precontract"] is not None
        and traceable
        and complete_cases
        and has_outputs
        and structure_valid
        and reactive_complete
        and invariant_coverage
        and not unknown_invariants
        and not conflicts
        and not rejected_zones
        and not reactive_errors
    )
    receipt = {
        "schema_version": "creative.quality.v1",
        "map_id": bound["map_id"], "map_hash": bound["map_hash"],
        "dimensions": score, "total": total, "threshold": threshold,
        "status": "READY_FOR_REVIEW" if total >= threshold and not hard_block else "BLOCKED",
        "evidence": evidence,
        "canonical_mutation": False,
    }
    receipt["receipt_hash"] = canonical_hash(receipt)
    return receipt


__all__ = ["QUALITY_WEIGHTS", "REACTIVE_CONTRACT_FIELDS", "REACTIVE_CONTRACT_VERSION", "evaluate_creative_quality", "missing_required_output_content", "missing_required_validation_cases"]
