"""Deterministic cross-zone synthesis receipt; proposal-only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from synapse.core.creative_contracts import (
    CREATIVE_REFERENCE_RELATIONS,
    validate_global_map_envelope,
    validate_zone_envelope,
)
from synapse.core.creative_quality import evaluate_creative_quality
from synapse.core.idea_session import canonical_hash

_REFERENCE_RELATIONS = CREATIVE_REFERENCE_RELATIONS


def verify_creative_synthesis_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a synthesis receipt without rebuilding or applying any output.

    This is intentionally a receipt-only check.  It detects ordinary receipt
    tampering and disagreement between the top-level identity and nested
    quality evidence; source-zone recomputation remains the responsibility of
    the runner, which is the only component holding the original zones.
    """

    supplied = receipt.get("synthesis_hash")
    payload = dict(receipt)
    payload.pop("synthesis_hash", None)
    quality = receipt.get("quality")
    quality_hash = quality.get("receipt_hash") if isinstance(quality, Mapping) else None
    quality_payload = dict(quality) if isinstance(quality, Mapping) else None
    if quality_payload is not None:
        quality_payload.pop("receipt_hash", None)
    quality_evidence = (
        quality.get("evidence")
        if isinstance(quality, Mapping) and isinstance(quality.get("evidence"), Mapping)
        else None
    )
    detailed_reactive = receipt.get("detailed_reactive")
    quality_detailed_reactive = (
        quality_evidence.get("detailed_reactive")
        if isinstance(quality_evidence, Mapping)
        else None
    )
    quality_reactive_versions = (
        quality_evidence.get("reactive_contract_versions")
        if isinstance(quality_evidence, Mapping)
        else None
    )
    checks = {
        "schema_supported": receipt.get("schema_version") == "creative.synthesis.v1",
        "precontract_hash_bound": isinstance(quality_evidence, Mapping) and receipt.get("precontract_hash") == quality_evidence.get("precontract_hash"),
        "synthesis_hash_present": isinstance(supplied, str) and supplied.startswith("sha256:"),
        "synthesis_hash_matches": isinstance(supplied, str) and canonical_hash(payload) == supplied,
        "quality_receipt_hash_bound": receipt.get("quality_receipt_hash") == quality_hash and isinstance(quality_hash, str),
        "quality_receipt_hash_matches": (
            isinstance(quality_payload, Mapping)
            and isinstance(quality_hash, str)
            and canonical_hash(quality_payload) == quality_hash
        ),
        "content_hashes_bound": isinstance(quality_evidence, Mapping) and receipt.get("zone_content_hashes") == quality_evidence.get("zone_content_hashes"),
        "reactive_status_bound": isinstance(quality_evidence, Mapping) and receipt.get("reactive_contract_status") == quality_evidence.get("reactive_contract_status"),
        "reactive_versions_bound": isinstance(quality, Mapping) and receipt.get("reactive_contract_versions") == quality_reactive_versions,
        "reactive_mode_bound": isinstance(detailed_reactive, bool) and detailed_reactive == quality_detailed_reactive,
        "proposal_only": receipt.get("canonical_mutation") is False and receipt.get("filesystem_mutation") is False and receipt.get("execution_allowed") is False,
    }
    errors = tuple(name for name, passed in checks.items() if not passed)
    return {"passed": not errors, "checks": checks, "errors": errors, "receipt": dict(receipt)}


def build_creative_synthesis_receipt(
    global_map: Mapping[str, Any], zones: Sequence[Mapping[str, Any]], *, detailed_reactive: bool = False
) -> dict[str, Any]:
    """Bind zone coverage, references, and quality evidence without applying output.

    ``detailed_reactive`` is opt-in and is forwarded to the quality gate so
    direct synthesis callers receive the same strict contract behavior as the
    ordered creative runner.
    """

    bound = validate_global_map_envelope(global_map)
    validated = [validate_zone_envelope(zone, global_map=global_map) for zone in zones]
    planned = tuple(bound["zone_plan"])
    by_type = {zone["zone_type"]: zone for zone in validated}
    if len(by_type) != len(validated) or set(by_type) != set(planned):
        raise ValueError("synthesis requires exactly one zone per zone_plan entry")
    known = set(planned)
    declared: dict[str, set[str]] = {}
    declared_owner: dict[str, str] = {}
    reference_errors: list[str] = []
    references: dict[str, list[str]] = {}
    dependency_edges: list[tuple[str, str]] = []
    for zone in validated:
        raw_declared = zone["outputs"].get("declared_ids", [])
        if not isinstance(raw_declared, list) or any(not isinstance(item, str) or not item.strip() for item in raw_declared):
            reference_errors.append(f"declared_ids_invalid:{zone['zone_type']}")
            raw_declared = []
        declared[zone["zone_type"]] = set(raw_declared)
        for item in raw_declared:
            if item in declared_owner:
                reference_errors.append(f"duplicate_declared_id:{item}")
            declared_owner[item] = zone["zone_type"]
        # ``cross_refs`` is the canonical typed reference field shared with
        # the consistency validator.  The former ``cross_zone_refs`` name is
        # intentionally not consumed so producers cannot silently diverge.
        raw_refs = zone["outputs"].get("cross_refs", [])
        if not isinstance(raw_refs, list):
            reference_errors.append(f"cross_refs_invalid:{zone['zone_type']}")
            continue
        refs: list[str] = []
        typed_ref_ids: set[str] = set()
        for raw_ref in raw_refs:
            if isinstance(raw_ref, Mapping):
                source_zone = raw_ref.get("source_zone")
                target_zone = raw_ref.get("target_zone")
                source_id = raw_ref.get("source_id")
                target_id = raw_ref.get("target_id")
                relation = raw_ref.get("relation", "depends_on")
                if (not isinstance(source_zone, str) or source_zone != zone["zone_type"]
                        or not isinstance(target_zone, str) or target_zone not in known
                        or not isinstance(source_id, str) or not source_id.strip()
                        or not isinstance(target_id, str) or not target_id.strip()
                        or not isinstance(relation, str) or relation not in _REFERENCE_RELATIONS):
                    reference_errors.append(f"typed_reference_invalid:{zone['zone_type']}")
                    continue
                if source_zone == target_zone:
                    reference_errors.append(f"typed_reference_self:{zone['zone_type']}")
                if relation == "depends_on":
                    dependency_edges.append((source_zone, target_zone))
                refs.append(f"{source_zone}:{source_id}->{target_zone}:{target_id}:{relation}")
                typed_ref_ids.add(refs[-1])
            else:
                refs.append(str(raw_ref))
        refs = sorted(set(refs))
        references[zone["zone_type"]] = refs
        reference_errors.extend(f"unknown_cross_ref:{zone['zone_type']}:{ref}" for ref in refs if ref not in known and ref not in declared_owner and ref not in typed_ref_ids and not "->" in ref)
        if "cross_zone_refs" in zone["outputs"]:
            reference_errors.append(f"cross_zone_refs_unsupported:{zone['zone_type']}")
    for source, target in dependency_edges:
        if (target, source) in dependency_edges:
            reference_errors.append(f"cross_zone_cycle:{source}:{target}")
    quality = evaluate_creative_quality(global_map, zones, detailed_reactive=detailed_reactive)
    identity = {"map_id": bound["map_id"], "map_hash": bound["map_hash"], "precontract_hash": bound["precontract_hash"], "zone_types": list(planned),
                "zone_ids": sorted(zone["zone_id"] for zone in validated), "declared_ids": {key: sorted(value) for key, value in sorted(declared.items())}, "references": references,
                "zone_content_hashes": quality["evidence"]["zone_content_hashes"],
                "reactive_contract_status": quality["evidence"]["reactive_contract_status"],
                "reactive_contract_versions": quality["evidence"]["reactive_contract_versions"],
                "quality_receipt_hash": quality["receipt_hash"], "reference_errors": sorted(set(reference_errors)),
                "detailed_reactive": detailed_reactive}
    status = "READY_FOR_REVIEW" if quality["status"] == "READY_FOR_REVIEW" and not reference_errors else "BLOCKED"
    receipt = {"schema_version": "creative.synthesis.v1", **identity, "quality": quality,
               "status": status, "canonical_mutation": False, "filesystem_mutation": False,
               "provider_called": False, "execution_allowed": False}
    receipt["synthesis_hash"] = canonical_hash(receipt)
    return receipt


__all__ = ["build_creative_synthesis_receipt", "verify_creative_synthesis_receipt"]
