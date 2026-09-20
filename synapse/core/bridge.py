"""Bridge verified inputs into existing Registry Proposal contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from synapse.core.candidate import CandidateMutation, VerificationReceipt
from synapse.core.contracts import Entity, LifecycleStatus, Provenance
from synapse.core.errors import InvariantViolation
from synapse.core.registry import Proposal, ProposalOperation
from synapse.core.scaffold import ScaffoldPlan


class ProposalBridgeError(ValueError):
    """Raised when a Candidate cannot cross the Proposal boundary."""


def candidate_to_proposal(
    candidate: CandidateMutation,
    receipt: VerificationReceipt,
    *,
    actor: str,
) -> Proposal:
    """Adapt one passing Candidate to an Entity CREATE Proposal, without commit."""
    if not actor.strip():
        raise ProposalBridgeError("Proposal actor가 비어 있습니다.")
    if receipt.candidate_id != candidate.id or not receipt.passed:
        raise ProposalBridgeError("통과한 VerificationReceipt 없이 Proposal을 만들 수 없습니다.")
    if candidate.status is not LifecycleStatus.PROPOSED:
        raise ProposalBridgeError("PROPOSED Candidate만 Proposal로 bridge할 수 있습니다.")
    changes: Mapping[str, Any] = candidate.changes
    name = str(changes.get("name", "")).strip()
    entity_type = str(changes.get("entity_type", "")).strip()
    if not name or not entity_type:
        raise ProposalBridgeError("Candidate changes에 name과 entity_type이 필요합니다.")
    status_raw = str(changes.get("status", LifecycleStatus.ASSUMED.value))
    try:
        status = LifecycleStatus(status_raw)
    except ValueError as exc:
        raise ProposalBridgeError(f"지원하지 않는 Entity status입니다: {status_raw}") from exc
    item_id = str(changes.get("id", f"entity:{candidate.id.removeprefix('candidate:')[:24]}"))
    attributes = changes.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise ProposalBridgeError("Entity attributes는 mapping이어야 합니다.")
    try:
        entity = Entity(
            id=item_id,
            canonical_key=str(changes.get("canonical_key", item_id)),
            owner=candidate.owner,
            name=name,
            entity_type=entity_type,
            status=status,
            priority=int(changes.get("priority", 0)),
            authority=str(changes.get("authority", "")).strip() or None,
            provenance=candidate.provenance,
            depends_on=tuple(str(value) for value in changes.get("depends_on", ())),
            conflicts_with=tuple(str(value) for value in changes.get("conflicts_with", ())),
            attributes=attributes,
            created_at=f"candidate:{candidate.id}",
            updated_at=f"candidate:{candidate.id}",
        )
    except (InvariantViolation, TypeError, ValueError) as exc:
        raise ProposalBridgeError(f"Candidate를 Entity로 변환할 수 없습니다: {exc}") from exc
    return Proposal(
        candidate=entity,
        operation=ProposalOperation.CREATE,
        actor=actor,
        id=f"proposal:{candidate.id}:create",
        created_at=f"candidate:{candidate.id}",
        rationale=str(changes.get("rationale", "Verified Candidate from Cognitive Table")),
    )


def scaffold_plan_to_proposal(
    plan: ScaffoldPlan,
    *,
    owner: str = "human",
    approved: bool,
    actor: str,
) -> Proposal:
    """Adapt an explicitly reviewed new-project plan to a Proposal.

    The plan is caller-provided input, so the project starts as ``ASSUMED``
    rather than ``CONFIRMED``.  This function creates no Canonical State and
    does not treat ``approved`` or ``actor`` as authentication; they are an
    explicit caller-owned review boundary, matching the existing proposal
    contract.
    """

    if not isinstance(plan, ScaffoldPlan):
        raise ProposalBridgeError("ScaffoldPlan이 필요합니다.")
    if not isinstance(approved, bool) or not approved:
        raise ProposalBridgeError(
            "new-project ScaffoldPlan은 approved=true인 명시적 검토 뒤에만 Proposal이 됩니다."
        )
    if not isinstance(owner, str) or not owner.strip():
        raise ProposalBridgeError("Scaffold project owner가 비어 있습니다.")
    if not isinstance(actor, str) or not actor.strip():
        raise ProposalBridgeError("Proposal actor가 비어 있습니다.")

    project_id = f"project:{plan.id}"
    source_id = f"input:idea:{plan.id}"
    entity = Entity(
        id=project_id,
        owner=owner.strip(),
        canonical_key=project_id,
        status=LifecycleStatus.ASSUMED,
        name=plan.project_name,
        entity_type="project",
        provenance=(
            # The idea is explicit caller input, not an observation or model
            # assertion.  Keeping it as provenance makes the eventual review
            # and confirmation boundary visible in Canonical State.
            Provenance(
                source_id=source_id,
                method="explicit_input",
                locator="idea",
                captured_at=f"scaffold:{plan.id}",
            ),
        ),
        attributes={
            "schema_version": "scaffold-plan.v1",
            "scaffold_plan_id": plan.id,
            "project_slug": plan.project_slug,
            "preset_id": plan.preset.id,
            "idea": plan.idea,
            "planned_paths": [item.path for item in plan.files],
            "required_count": plan.required_count,
        },
        created_at=f"scaffold:{plan.id}",
        updated_at=f"scaffold:{plan.id}",
    )
    return Proposal(
        candidate=entity,
        operation=ProposalOperation.CREATE,
        actor=actor.strip(),
        id=f"proposal:{project_id}:create",
        created_at=f"scaffold:{plan.id}",
        rationale="Explicitly reviewed new-project scaffold plan.",
    )


__all__ = ["ProposalBridgeError", "candidate_to_proposal", "scaffold_plan_to_proposal"]
