"""Review-only bridge from a creative quality receipt to existing verification UI contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from synapse.core.guided_work_unit_candidate import GuidedWorkUnitVerification
from synapse.core.idea_session import canonical_hash, canonical_json


def verify_creative_quality_receipt(receipt: Mapping[str, Any]) -> GuidedWorkUnitVerification:
    """Convert a quality receipt into an immutable, non-executing verification record."""

    evidence = receipt.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    total = receipt.get("total")
    threshold = receipt.get("threshold")
    threshold_met = (
        type(total) is int
        and 0 <= total <= 100
        and type(threshold) is int
        and 0 <= threshold <= 100
        and total >= threshold
    )
    checks = {
        "schema_supported": receipt.get("schema_version") == "creative.quality.v1",
        "review_ready": receipt.get("status") == "READY_FOR_REVIEW",
        "threshold_met": threshold_met,
        "no_uncovered_invariants": not evidence.get("uncovered_invariants"),
        "no_conflicts": not evidence.get("conflicts"),
        "no_retry_zones": not evidence.get("retry_zones"),
        "structure_valid": evidence.get("structure_valid") is True
        and not evidence.get("structure_validation_errors"),
        "canonical_mutation_false": receipt.get("canonical_mutation") is False,
        "receipt_hash_present": isinstance(receipt.get("receipt_hash"), str) and receipt["receipt_hash"].startswith("sha256:"),
        # The evidence is part of the signed receipt.  Validate its reactive
        # and content-hash shape as well, so a malformed receipt cannot be
        # treated as reviewable merely because it has a hash-shaped string.
        "evidence_hashes_well_formed": _quality_evidence_hashes_well_formed(receipt),
    }
    if checks["receipt_hash_present"]:
        payload = dict(receipt)
        supplied_hash = payload.pop("receipt_hash")
        checks["receipt_hash_matches"] = canonical_hash(payload) == supplied_hash
    else:
        checks["receipt_hash_matches"] = False
    errors = tuple(key for key, passed in checks.items() if not passed)
    receipt_id = receipt.get("receipt_hash") if isinstance(receipt.get("receipt_hash"), str) else "creative-quality:invalid"
    identity = {"receipt": dict(receipt), "checks": checks, "errors": errors}
    verification_id = "creative-quality-verification:" + hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return GuidedWorkUnitVerification(
        id=verification_id, candidate_id=receipt_id,
        passed=not errors and all(checks.values()), checks=checks, errors=errors,
    )


__all__ = ["verify_creative_quality_receipt"]


def _quality_evidence_hashes_well_formed(receipt: Mapping[str, Any]) -> bool:
    evidence = receipt.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    content_hashes = evidence.get("zone_content_hashes")
    reactive_status = evidence.get("reactive_contract_status")
    if not isinstance(content_hashes, Mapping) or not isinstance(reactive_status, Mapping):
        return False
    if set(content_hashes) != set(reactive_status):
        return False
    return all(isinstance(value, str) and value.startswith("sha256:") for value in content_hashes.values())
