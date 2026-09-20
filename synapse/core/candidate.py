"""Candidate Mutation and deterministic verification boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.action import ActionRoute
from synapse.core.contracts import LifecycleStatus, Provenance
from synapse.core.table import CognitiveTable


class CandidateError(ValueError):
    """Raised when a Candidate Mutation is structurally invalid."""


@dataclass(frozen=True, slots=True)
class CandidateMutation:
    id: str
    action_id: str
    table_id: str
    owner: str
    changes: Mapping[str, Any]
    evidence_ids: tuple[str, ...]
    status: LifecycleStatus
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.action_id.strip() or not self.table_id.strip() or not self.owner.strip():
            raise CandidateError("Candidate 식별자와 owner가 필요합니다.")
        if not isinstance(self.status, LifecycleStatus):
            raise CandidateError("Candidate status는 LifecycleStatus여야 합니다.")
        object.__setattr__(self, "changes", MappingProxyType(dict(self.changes)))
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))
        object.__setattr__(self, "provenance", tuple(self.provenance))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "action_id": self.action_id,
            "table_id": self.table_id,
            "owner": self.owner,
            "changes": dict(self.changes),
            "evidence_ids": list(self.evidence_ids),
            "status": self.status.value,
            "provenance": [
                {
                    "source_id": entry.source_id,
                    "method": entry.method,
                    "id": entry.id,
                    "locator": entry.locator,
                    "excerpt": entry.excerpt,
                    "captured_at": entry.captured_at,
                    "confidence": entry.confidence,
                }
                for entry in self.provenance
            ],
        }


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    id: str
    candidate_id: str
    passed: bool
    checks: Mapping[str, bool]
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.candidate_id.strip():
            raise CandidateError("VerificationReceipt 식별자가 필요합니다.")
        object.__setattr__(self, "checks", MappingProxyType(dict(sorted(self.checks.items()))))
        object.__setattr__(self, "errors", tuple(sorted(set(self.errors))))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "passed": self.passed,
            "checks": dict(self.checks),
            "errors": list(self.errors),
            "canonical_mutation": False,
        }


def candidate_from_table(
    table: CognitiveTable,
    route: ActionRoute,
    *,
    owner: str,
    changes: Mapping[str, Any],
    evidence_ids: tuple[str, ...] = (),
) -> CandidateMutation:
    """Convert a worker-shaped result into a PROPOSED/blocked candidate only."""
    if not owner.strip():
        raise CandidateError("Candidate owner가 비어 있습니다.")
    requested_table = route.request.attributes.get("table_id")
    if requested_table is not None and requested_table != table.id:
        raise CandidateError("ActionRoute가 다른 Cognitive Table을 가리킵니다.")
    status = LifecycleStatus.PROPOSED
    if route.status == "BLOCKED" or table.conflicts:
        status = LifecycleStatus.BLOCKED
    elif table.unresolved:
        status = LifecycleStatus.UNRESOLVED
    source_id = f"source:action:{route.request.id}"
    provenance = (
        Provenance(
            source_id=source_id,
            method="action_router_candidate",
            captured_at=f"action:{route.request.id}",
        ),
    )
    canonical = json.dumps(
        {
            "action_id": route.request.id,
            "table_id": table.id,
            "owner": owner,
            "changes": dict(changes),
            "evidence_ids": sorted(set(evidence_ids)),
            "status": status.value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CandidateMutation(
        id=f"candidate:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        action_id=route.request.id,
        table_id=table.id,
        owner=owner,
        changes=changes,
        evidence_ids=evidence_ids,
        status=status,
        provenance=provenance,
    )


def verify_candidate(
    candidate: CandidateMutation,
    *,
    table: CognitiveTable | None = None,
    route: ActionRoute | None = None,
) -> VerificationReceipt:
    """Run deterministic checks; this function never commits or mutates state."""
    checks: dict[str, bool] = {
        "proposed_status": candidate.status is LifecycleStatus.PROPOSED,
        "owner_present": bool(candidate.owner.strip()),
        "changes_present": bool(candidate.changes),
        "evidence_present": bool(candidate.evidence_ids),
        "provenance_present": bool(candidate.provenance),
    }
    errors: list[str] = []
    if table is not None:
        checks["table_matches"] = candidate.table_id == table.id
        checks["no_table_conflict"] = not table.conflicts
        checks["no_table_unresolved"] = not table.unresolved
        if candidate.table_id != table.id:
            errors.append("candidate_table_mismatch")
        if table.conflicts:
            errors.append("table_conflict_unresolved")
        if table.unresolved:
            errors.append("table_unresolved_preserved")
    if route is not None:
        checks["route_ready"] = route.status == "READY"
        if route.status != "READY":
            errors.append(f"action_route_{route.status.lower()}")
    errors.extend(key for key, passed in checks.items() if not passed)
    canonical = json.dumps(
        {
            "candidate_id": candidate.id,
            "checks": checks,
            "errors": sorted(set(errors)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return VerificationReceipt(
        id=f"verify:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        candidate_id=candidate.id,
        passed=not errors,
        checks=checks,
        errors=tuple(errors),
    )


__all__ = ["CandidateError", "CandidateMutation", "VerificationReceipt", "candidate_from_table", "verify_candidate"]
