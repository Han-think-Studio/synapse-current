"""Deterministic detail slots for the Guided Project flow.

The existing scaffold presets describe *where* a project can be written.  This
module describes *what must be decided before writing* and how each item may be
filled.  The general pack is always present; a selected domain pack only adds
detail slots.  The planner is proposal-only and keeps missing, proposed,
accepted, and deferred work explicit so a project can be used before optional
detail is complete and resumed later without losing the original seed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.idea_expansion import IdeaExpansionProposal
from synapse.core.idea_session import IdeaSeed
from synapse.core.preset_catalog import load_preset_catalog
from synapse.core.structure_map import StructureMapProposal


class GuidedDetailError(ValueError):
    """Raised when a detail-plan request is incomplete or unsafe."""


DETAIL_STATUSES = {"MISSING", "PROPOSED", "ACCEPTED", "DEFERRED"}
DETAIL_MODES = {"user_text", "guided_choice", "llm_draft", "accept_proposal", "defer"}
DETAIL_CATEGORIES = {"general", "software", "research", "content", "automation"}


def _text(value: Any, label: str, *, required: bool = True, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        raise GuidedDetailError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise GuidedDetailError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise GuidedDetailError(f"{label}이(가) 너무 깁니다.")
    return result


def _strings(value: Sequence[str], label: str, *, limit: int = 240) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise GuidedDetailError(f"{label}는 문자열 배열이어야 합니다.")
    result = tuple(_text(item, label, limit=limit) for item in value)
    if len(set(result)) != len(result):
        raise GuidedDetailError(f"{label}에 중복 항목이 있습니다.")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class DetailSlot:
    """One fillable decision, independent of a physical file."""

    id: str
    category_id: str
    title: str
    purpose: str
    required: bool
    artifact_role: str
    answer_mode: str
    completion_criteria: str
    question: str
    why_needed: str
    suggestions: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    status: str = "MISSING"
    source_refs: tuple[str, ...] = ()
    candidate_summary: str = ""

    def __post_init__(self) -> None:
        _text(self.id, "slot.id", limit=160)
        if self.category_id not in DETAIL_CATEGORIES:
            raise GuidedDetailError(f"지원하지 않는 detail category입니다: {self.category_id}")
        for label, value in (
            ("slot.title", self.title),
            ("slot.purpose", self.purpose),
            ("slot.completion_criteria", self.completion_criteria),
            ("slot.question", self.question),
            ("slot.why_needed", self.why_needed),
            ("slot.artifact_role", self.artifact_role),
        ):
            _text(value, label)
        if self.answer_mode not in DETAIL_MODES - {"accept_proposal", "defer"}:
            raise GuidedDetailError(f"지원하지 않는 detail answer_mode입니다: {self.answer_mode}")
        if self.status not in DETAIL_STATUSES:
            raise GuidedDetailError(f"지원하지 않는 detail status입니다: {self.status}")
        object.__setattr__(self, "suggestions", _strings(self.suggestions, "slot.suggestions"))
        object.__setattr__(self, "depends_on", _strings(self.depends_on, "slot.depends_on", limit=160))
        object.__setattr__(self, "source_refs", _strings(self.source_refs, "slot.source_refs", limit=160))
        object.__setattr__(self, "candidate_summary", self.candidate_summary.strip())

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category_id": self.category_id,
            "title": self.title,
            "purpose": self.purpose,
            "required": self.required,
            "artifact_role": self.artifact_role,
            "answer_mode": self.answer_mode,
            "completion_criteria": self.completion_criteria,
            "question": self.question,
            "why_needed": self.why_needed,
            "suggestions": list(self.suggestions),
            "depends_on": list(self.depends_on),
            "status": self.status,
            "source_refs": list(self.source_refs),
            "candidate_summary": self.candidate_summary,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DetailQuestion:
    """A bounded question that lets a person choose how a slot is filled."""

    id: str
    slot_id: str
    question: str
    why_needed: str
    status: str
    blocking: bool
    allowed_modes: tuple[str, ...]
    suggestions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.id, "question.id", limit=160)
        _text(self.slot_id, "question.slot_id", limit=160)
        _text(self.question, "question.question")
        _text(self.why_needed, "question.why_needed")
        if self.status not in DETAIL_STATUSES:
            raise GuidedDetailError(f"지원하지 않는 question status입니다: {self.status}")
        modes = _strings(self.allowed_modes, "question.allowed_modes", limit=80)
        if any(mode not in DETAIL_MODES for mode in modes):
            raise GuidedDetailError("question에 허용되지 않은 fill mode가 있습니다.")
        object.__setattr__(self, "allowed_modes", modes)
        object.__setattr__(self, "suggestions", _strings(self.suggestions, "question.suggestions"))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "question": self.question,
            "why_needed": self.why_needed,
            "status": self.status,
            "blocking": self.blocking,
            "allowed_modes": list(self.allowed_modes),
            "suggestions": list(self.suggestions),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedDetailPlan:
    """A resumable, memory-only plan for filling the project structure."""

    id: str
    seed_hash: str
    base_preset_id: str
    category_ids: tuple[str, ...]
    revision: int
    slots: tuple[DetailSlot, ...]
    questions: tuple[DetailQuestion, ...]
    coverage: Mapping[str, int]
    first_missing_slot_id: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.seed_hash.strip():
            raise GuidedDetailError("detail plan 식별자와 seed hash가 필요합니다.")
        if self.base_preset_id != "general":
            raise GuidedDetailError("detail plan의 기본 프리셋은 general이어야 합니다.")
        if self.revision < 1:
            raise GuidedDetailError("detail revision은 1 이상이어야 합니다.")
        categories = _strings(self.category_ids, "category_ids", limit=80)
        if not categories or categories[0] != "general" or len(set(categories)) != len(categories):
            raise GuidedDetailError("general 기본 카테고리가 먼저 있어야 하며 category가 중복될 수 없습니다.")
        if any(category not in DETAIL_CATEGORIES for category in categories):
            raise GuidedDetailError("지원하지 않는 detail category가 있습니다.")
        slots = tuple(self.slots)
        questions = tuple(self.questions)
        if len({slot.id for slot in slots}) != len(slots):
            raise GuidedDetailError("detail slot ID가 중복됩니다.")
        if len({question.id for question in questions}) != len(questions):
            raise GuidedDetailError("detail question ID가 중복됩니다.")
        slot_ids = {slot.id for slot in slots}
        if any(question.slot_id not in slot_ids for question in questions):
            raise GuidedDetailError("detail question이 존재하지 않는 slot을 참조합니다.")
        if self.first_missing_slot_id is not None and self.first_missing_slot_id not in slot_ids:
            raise GuidedDetailError("first_missing_slot_id가 존재하지 않습니다.")
        object.__setattr__(self, "category_ids", categories)
        object.__setattr__(self, "slots", slots)
        object.__setattr__(self, "questions", questions)
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def required_missing(self) -> int:
        return int(self.coverage.get("required_missing", 0))

    @property
    def can_start(self) -> bool:
        return self.required_missing == 0

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seed_hash": self.seed_hash,
            "base_preset_id": self.base_preset_id,
            "category_ids": list(self.category_ids),
            "revision": self.revision,
            "slots": [slot.to_record() for slot in self.slots],
            "questions": [question.to_record() for question in self.questions],
            "coverage": dict(self.coverage),
            "first_missing_slot_id": self.first_missing_slot_id,
            "readiness": {"can_start": self.can_start, "required_missing": self.required_missing},
            "metadata": dict(self.metadata),
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "model_invoked": False,
        }


def list_guided_categories() -> tuple[dict[str, Any], ...]:
    """Return stable category choices; ``general`` is always the base layer."""

    return tuple(category.to_category_record() for category in load_preset_catalog().categories)


def _slot_specs(category: str) -> tuple[dict[str, Any], ...]:
    try:
        return tuple(slot.to_spec() for slot in load_preset_catalog().category(category).slots)
    except KeyError:
        raise GuidedDetailError(f"지원하지 않는 detail category입니다: {category}")


def _normalize_categories(category_ids: Sequence[str] | None, preset_hint: str = "general") -> tuple[str, ...]:
    raw = list(category_ids or [])
    if not raw:
        raw = [preset_hint or "general"]
    normalized: list[str] = ["general"]
    for item in raw:
        category = _text(item, "category_id", limit=80).lower()
        if category not in DETAIL_CATEGORIES:
            raise GuidedDetailError(f"지원하지 않는 detail category입니다: {category}")
        if category not in normalized:
            normalized.append(category)
    return tuple(normalized)


def _candidate_from_expansion(expansion: IdeaExpansionProposal | None, slot_id: str) -> tuple[str, ...] | None:
    if expansion is None:
        return None
    values: dict[str, tuple[str, ...]] = {
        "general.goal": (expansion.intended_outcome, *expansion.success_criteria),
        "general.scope": (*expansion.scope_in, *expansion.scope_out, *expansion.constraints),
        "general.entities": expansion.concepts,
        "general.rules": expansion.constraints,
        "general.next_step": expansion.workflows,
        "content.subjects": (*expansion.audience, *expansion.concepts),
        "content.continuity": expansion.constraints,
        "content.outline": expansion.workflows,
        "software.validation": expansion.success_criteria,
        "research.sources": expansion.concepts,
        "automation.inputs_outputs": expansion.workflows,
    }
    result = tuple(item.strip() for item in values.get(slot_id, ()) if item.strip())
    return result or None


def _candidate_from_structure(structure: StructureMapProposal | None, artifact_role: str) -> tuple[tuple[str, ...], str] | None:
    if structure is None:
        return None
    matches = [node for node in structure.nodes if node.artifact_role == artifact_role]
    if not matches:
        return None
    refs = tuple(node.id for node in matches)
    summary = " / ".join(node.purpose for node in matches)
    return refs, summary


def _answer_value(raw: Any, slot_id: str) -> tuple[str, str, tuple[str, ...]] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        value = raw.strip()
        return (value, "ACCEPTED", ("user_answer",)) if value else None
    if not isinstance(raw, Mapping):
        raise GuidedDetailError(f"answers[{slot_id}]는 문자열 또는 객체여야 합니다.")
    unknown = set(raw) - {"value", "status", "source_refs"}
    if unknown:
        raise GuidedDetailError(f"answers[{slot_id}]에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    value = _text(raw.get("value", ""), f"answers[{slot_id}].value", required=False)
    status = str(raw.get("status", "ACCEPTED")).strip().upper()
    if status not in {"ACCEPTED", "PROPOSED", "DEFERRED"}:
        raise GuidedDetailError(f"answers[{slot_id}].status가 올바르지 않습니다.")
    refs = _strings(raw.get("source_refs", ["user_answer"]), f"answers[{slot_id}].source_refs", limit=160)
    if status != "DEFERRED" and not value:
        return None
    return value, status, refs


def build_guided_detail_plan(
    seed: IdeaSeed,
    *,
    category_ids: Sequence[str] | None = None,
    expansion: IdeaExpansionProposal | None = None,
    structure: StructureMapProposal | None = None,
    answers: Mapping[str, Any] | None = None,
    revision: int = 1,
) -> GuidedDetailPlan:
    """Build a stable base+category fill plan without model or filesystem access."""

    if not isinstance(seed, IdeaSeed):
        raise GuidedDetailError("detail plan에는 IdeaSeed가 필요합니다.")
    categories = _normalize_categories(category_ids, seed.preset_hint)
    if expansion is not None and expansion.seed_hash != seed.raw_hash:
        raise GuidedDetailError("IdeaExpansion seed hash가 IdeaSeed와 다릅니다.")
    if structure is not None and structure.seed_hash != seed.raw_hash:
        raise GuidedDetailError("StructureMap seed hash가 IdeaSeed와 다릅니다.")
    if expansion is not None and structure is not None and structure.expansion_id != expansion.id:
        raise GuidedDetailError("StructureMap이 다른 IdeaExpansion을 참조합니다.")
    answer_map = answers or {}
    if not isinstance(answer_map, Mapping):
        raise GuidedDetailError("answers는 객체여야 합니다.")

    raw_slots: list[dict[str, Any]] = []
    for category in categories:
        raw_slots.extend(_slot_specs(category))
    slots: list[DetailSlot] = []
    for spec in raw_slots:
        slot_id = str(spec["id"])
        status = "MISSING"
        refs: tuple[str, ...] = ()
        candidate_summary = ""
        answer = _answer_value(answer_map.get(slot_id), slot_id)
        if answer is not None:
            candidate_summary, status, refs = answer
        else:
            expansion_values = _candidate_from_expansion(expansion, slot_id)
            structure_match = _candidate_from_structure(structure, str(spec["artifact_role"]))
            if expansion_values:
                candidate_summary = " / ".join(expansion_values)
                status = "PROPOSED"
                refs = ("expansion",)
            if structure_match:
                node_refs, structure_summary = structure_match
                candidate_summary = " / ".join(item for item in (candidate_summary, structure_summary) if item)
                status = "PROPOSED"
                refs = tuple(dict.fromkeys((*refs, *node_refs)))
        slots.append(
            DetailSlot(
                **spec,
                status=status,
                source_refs=refs,
                candidate_summary=candidate_summary,
            )
        )

    questions: list[DetailQuestion] = []
    for slot in slots:
        if slot.status == "ACCEPTED":
            continue
        modes = ("accept_proposal", "user_text", "guided_choice", "llm_draft", "defer") if slot.status == "PROPOSED" else ("user_text", "guided_choice", "llm_draft", "defer")
        questions.append(
            DetailQuestion(
                id=f"detail-question:{slot.id}",
                slot_id=slot.id,
                question=slot.question if slot.status == "MISSING" else f"{slot.title} 제안을 어떻게 채택할까요?",
                why_needed=slot.why_needed,
                status=slot.status,
                blocking=slot.required,
                allowed_modes=modes,
                suggestions=slot.suggestions,
            )
        )
    required_missing = sum(slot.required and slot.status != "ACCEPTED" for slot in slots)
    proposed_pending = sum(slot.status == "PROPOSED" for slot in slots)
    optional_missing = sum((not slot.required) and slot.status == "MISSING" for slot in slots)
    accepted = sum(slot.status == "ACCEPTED" for slot in slots)
    deferred = sum(slot.status == "DEFERRED" for slot in slots)
    coverage = {
        "total": len(slots),
        "accepted": accepted,
        "proposed": proposed_pending,
        "required_missing": required_missing,
        "optional_missing": optional_missing,
        "deferred": deferred,
    }
    first_missing = next((slot.id for slot in slots if slot.status != "ACCEPTED"), None)
    identity = {
        "seed_hash": seed.raw_hash,
        "categories": categories,
        "revision": revision,
        "slots": [slot.to_record() for slot in slots],
        "questions": [question.to_record() for question in questions],
    }
    plan_id = f"detail-plan:{hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}"
    metadata = {
        "compiler": "phase28e.guided_detail.v1",
        "base_layer": "general",
        "category_layer_count": len(categories) - 1,
        "model_invoked": False,
        "filesystem_mutation": False,
        "canonical_mutation": False,
        "resume_safe": True,
    }
    return GuidedDetailPlan(
        id=plan_id,
        seed_hash=seed.raw_hash,
        base_preset_id="general",
        category_ids=categories,
        revision=revision,
        slots=tuple(slots),
        questions=tuple(questions),
        coverage=coverage,
        first_missing_slot_id=first_missing,
        metadata=metadata,
    )


__all__ = [
    "DETAIL_CATEGORIES",
    "DETAIL_MODES",
    "DETAIL_STATUSES",
    "DetailQuestion",
    "DetailSlot",
    "GuidedDetailError",
    "GuidedDetailPlan",
    "build_guided_detail_plan",
    "list_guided_categories",
]
