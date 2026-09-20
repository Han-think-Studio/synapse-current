"""Proposal-only first WorkUnit planning for Guided detail sessions.

This module turns the verified detail plan into one bounded next action.  It
does not generate content, call a model, create a file, or execute anything.
It also computes a small deterministic diff so later slot answers can replace
the recommendation without mutating an earlier plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.guided_detail import GuidedDetailPlan
from synapse.core.idea_session import canonical_hash
from synapse.core.structure_map import ARTIFACT_ROLE_PATHS, StructureMapProposal


class GuidedWorkUnitError(ValueError):
    """Raised when a first WorkUnit preview cannot be made safely."""


def _text(value: Any, label: str, *, required: bool = True, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        raise GuidedWorkUnitError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise GuidedWorkUnitError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise GuidedWorkUnitError(f"{label}이(가) 너무 깁니다.")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedDetailDiff:
    """Deterministic slot-level diff between two detail-plan revisions."""

    base_plan_id: str | None
    current_plan_id: str
    added_slot_ids: tuple[str, ...] = ()
    changed_slot_ids: tuple[str, ...] = ()
    removed_slot_ids: tuple[str, ...] = ()
    newly_ready: bool = False
    newly_blocked: bool = False

    def to_record(self) -> dict[str, Any]:
        return {
            "base_plan_id": self.base_plan_id,
            "current_plan_id": self.current_plan_id,
            "added_slot_ids": list(self.added_slot_ids),
            "changed_slot_ids": list(self.changed_slot_ids),
            "removed_slot_ids": list(self.removed_slot_ids),
            "newly_ready": self.newly_ready,
            "newly_blocked": self.newly_blocked,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedWorkUnitPreview:
    """One review-only next action derived from a detail plan."""

    id: str
    seed_hash: str
    detail_plan_id: str
    detail_revision: int
    status: str
    title: str
    purpose: str
    source_slot_ids: tuple[str, ...]
    source_node_ids: tuple[str, ...]
    artifact_roles: tuple[str, ...]
    paths: tuple[str, ...]
    inputs: Mapping[str, Any]
    blocked_by: tuple[str, ...]
    completion_criteria: str
    downstream_impacts: tuple[str, ...]
    remaining_slot_ids: tuple[str, ...]
    structure_work_unit_id: str | None = None
    review_only: bool = True
    execution_allowed: bool = False
    canonical_mutation: bool = False
    filesystem_mutation: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.id, "work_unit_preview.id", limit=220)
        _text(self.seed_hash, "work_unit_preview.seed_hash", limit=120)
        _text(self.detail_plan_id, "work_unit_preview.detail_plan_id", limit=220)
        if self.status not in {"BLOCKED", "READY"}:
            raise GuidedWorkUnitError(f"지원하지 않는 WorkUnit preview 상태입니다: {self.status}")
        for label, value in (("title", self.title), ("purpose", self.purpose), ("completion_criteria", self.completion_criteria)):
            _text(value, f"work_unit_preview.{label}")
        roles = tuple(dict.fromkeys(self.artifact_roles))
        if any(role not in ARTIFACT_ROLE_PATHS for role in roles):
            raise GuidedWorkUnitError("WorkUnit preview에 알 수 없는 artifact role이 있습니다.")
        expected_paths = tuple(sorted({ARTIFACT_ROLE_PATHS[role] for role in roles}))
        if tuple(self.paths) != expected_paths:
            raise GuidedWorkUnitError("WorkUnit preview paths는 artifact role에서 결정되어야 합니다.")
        if self.status == "BLOCKED" and not self.blocked_by:
            raise GuidedWorkUnitError("BLOCKED WorkUnit preview에는 blocked_by가 필요합니다.")
        if self.status == "READY" and self.blocked_by:
            raise GuidedWorkUnitError("READY WorkUnit preview에는 blocked_by가 있을 수 없습니다.")
        if not self.review_only or self.execution_allowed or self.canonical_mutation or self.filesystem_mutation:
            raise GuidedWorkUnitError("WorkUnit preview는 review-only이며 mutation/execution을 허용하지 않습니다.")
        object.__setattr__(self, "source_slot_ids", tuple(dict.fromkeys(self.source_slot_ids)))
        object.__setattr__(self, "source_node_ids", tuple(dict.fromkeys(self.source_node_ids)))
        object.__setattr__(self, "artifact_roles", roles)
        object.__setattr__(self, "paths", expected_paths)
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        object.__setattr__(self, "blocked_by", tuple(dict.fromkeys(self.blocked_by)))
        object.__setattr__(self, "downstream_impacts", tuple(dict.fromkeys(self.downstream_impacts)))
        object.__setattr__(self, "remaining_slot_ids", tuple(dict.fromkeys(self.remaining_slot_ids)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seed_hash": self.seed_hash,
            "detail_plan_id": self.detail_plan_id,
            "detail_revision": self.detail_revision,
            "status": self.status,
            "title": self.title,
            "purpose": self.purpose,
            "source_slot_ids": list(self.source_slot_ids),
            "source_node_ids": list(self.source_node_ids),
            "artifact_roles": list(self.artifact_roles),
            "paths": list(self.paths),
            "inputs": dict(self.inputs),
            "blocked_by": list(self.blocked_by),
            "completion_criteria": self.completion_criteria,
            "downstream_impacts": list(self.downstream_impacts),
            "remaining_slot_ids": list(self.remaining_slot_ids),
            "structure_work_unit_id": self.structure_work_unit_id,
            "review_only": self.review_only,
            "execution_allowed": self.execution_allowed,
            "canonical_mutation": self.canonical_mutation,
            "filesystem_mutation": self.filesystem_mutation,
            "metadata": dict(self.metadata),
        }


def compute_guided_detail_diff(
    previous: GuidedDetailPlan | None,
    current: GuidedDetailPlan,
) -> GuidedDetailDiff:
    """Compare slots and readiness; never trust client-provided diff fields."""

    old = {slot.id: slot.to_record() for slot in previous.slots} if previous else {}
    new = {slot.id: slot.to_record() for slot in current.slots}
    return GuidedDetailDiff(
        base_plan_id=previous.id if previous else None,
        current_plan_id=current.id,
        added_slot_ids=tuple(sorted(set(new) - set(old))),
        changed_slot_ids=tuple(sorted(slot_id for slot_id in set(old) & set(new) if old[slot_id] != new[slot_id])),
        removed_slot_ids=tuple(sorted(set(old) - set(new))),
        newly_ready=bool(current.can_start and (previous is None or not previous.can_start)),
        newly_blocked=bool(previous is not None and previous.can_start and not current.can_start),
    )


def _input_record(slot) -> dict[str, Any]:
    return {
        "status": slot.status,
        "candidate_summary": slot.candidate_summary,
        "completion_criteria": slot.completion_criteria,
        "source_refs": list(slot.source_refs),
    }


def build_first_work_unit_preview(
    plan: GuidedDetailPlan,
    *,
    structure: StructureMapProposal | None = None,
) -> GuidedWorkUnitPreview:
    """Select one bounded next action from a detail plan and optional map."""

    if not isinstance(plan, GuidedDetailPlan):
        raise GuidedWorkUnitError("WorkUnit preview에는 GuidedDetailPlan이 필요합니다.")
    if structure is not None and structure.seed_hash != plan.seed_hash:
        raise GuidedWorkUnitError("StructureMap seed hash가 detail plan과 다릅니다.")
    slots = {slot.id: slot for slot in plan.slots}
    blocked = tuple(slot.id for slot in plan.slots if slot.required and slot.status != "ACCEPTED")
    pending = tuple(slot.id for slot in plan.slots if slot.status != "ACCEPTED")
    structure_unit = structure.work_units[0] if structure and structure.work_units and not blocked else None
    target_slot = slots[blocked[0]] if blocked else (slots[pending[0]] if pending else slots.get("general.next_step"))

    if blocked and target_slot is not None:
        status = "BLOCKED"
        title = f"필수 항목 채우기: {target_slot.title}"
        purpose = target_slot.purpose
        source_ids = (target_slot.id,)
        roles = (target_slot.artifact_role,)
        completion = target_slot.completion_criteria
        impacts = tuple(slot.id for slot in plan.slots if slot.id != target_slot.id and slot.status != "ACCEPTED")
        structure_id = None
    elif structure_unit is not None:
        status = "READY"
        title = structure_unit.title
        purpose = structure_unit.purpose
        source_ids = tuple(structure_unit.source_node_ids)
        roles = tuple(structure_unit.artifact_roles) or ("next_steps",)
        completion = structure_unit.completion_criteria
        impacts = tuple(structure_unit.downstream_impacts)
        structure_id = structure_unit.id
    elif target_slot is not None:
        status = "READY"
        title = f"세부 보강: {target_slot.title}" if pending else "프로젝트 사용 시작"
        purpose = target_slot.purpose
        source_ids = (target_slot.id,)
        roles = (target_slot.artifact_role,)
        completion = target_slot.completion_criteria
        impacts = tuple(slot.id for slot in plan.slots if slot.id != target_slot.id and slot.status != "ACCEPTED")
        structure_id = None
    else:
        raise GuidedWorkUnitError("detail plan에서 첫 WorkUnit 후보를 결정할 수 없습니다.")

    role_tuple = tuple(dict.fromkeys(roles))
    paths = tuple(sorted({ARTIFACT_ROLE_PATHS[role] for role in role_tuple}))
    input_ids = tuple(item for item in source_ids if item in slots)
    node_ids = tuple(structure_unit.source_node_ids) if structure_unit is not None else ()
    inputs = {slot_id: _input_record(slots[slot_id]) for slot_id in input_ids}
    identity = {
        "seed_hash": plan.seed_hash,
        "detail_plan_id": plan.id,
        "target": title,
        "status": status,
        "source_slot_ids": source_ids,
        "source_node_ids": node_ids,
        "roles": role_tuple,
        "structure_work_unit_id": structure_id,
    }
    return GuidedWorkUnitPreview(
        id=f"guided-work-unit:{canonical_hash(identity)}",
        seed_hash=plan.seed_hash,
        detail_plan_id=plan.id,
        detail_revision=plan.revision,
        status=status,
        title=title,
        purpose=purpose,
        source_slot_ids=input_ids,
        source_node_ids=node_ids,
        artifact_roles=role_tuple,
        paths=paths,
        inputs=inputs,
        blocked_by=blocked,
        completion_criteria=completion,
        downstream_impacts=impacts,
        remaining_slot_ids=tuple(item for item in pending if item not in input_ids),
        structure_work_unit_id=structure_id,
        metadata={
            "compiler": "phase28e.guided_work_unit.v1",
            "candidate_status": "PROPOSED",
            "model_invoked": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "work_unit_execution": False,
        },
    )


__all__ = [
    "GuidedDetailDiff",
    "GuidedWorkUnitError",
    "GuidedWorkUnitPreview",
    "build_first_work_unit_preview",
    "compute_guided_detail_diff",
]
