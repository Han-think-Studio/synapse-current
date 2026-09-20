"""Phase 28A Guided Project state machine and approval guards."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from synapse.core.idea_session import GUIDED_STAGES, GuidedProjectSession, IdeaSeed, utc_now


class GuidedTransitionError(ValueError):
    """Raised when a Guided session attempts an unsafe or skipped transition."""


class GuidedStage(str, Enum):
    WELCOME = "WELCOME"
    IDEA_CAPTURE = "IDEA_CAPTURE"
    IDEA_EXPANSION_READY = "IDEA_EXPANSION_READY"
    EXPANSION_REVIEW = "EXPANSION_REVIEW"
    QUESTION_ROUND = "QUESTION_ROUND"
    STRUCTURE_MAP_READY = "STRUCTURE_MAP_READY"
    STRUCTURE_REVIEW = "STRUCTURE_REVIEW"
    STRUCTURE_APPROVED = "STRUCTURE_APPROVED"
    FIRST_WORK_UNIT = "FIRST_WORK_UNIT"
    WORK_UNIT_REVIEW = "WORK_UNIT_REVIEW"
    NEXT_WORK_UNIT = "NEXT_WORK_UNIT"
    PROJECT_READY = "PROJECT_READY"


@dataclass(frozen=True, slots=True, kw_only=True)
class TransitionContext:
    """Facts supplied by deterministic validators; never inferred by a model."""

    seed_ready: bool = False
    dispatch_approved: bool = False
    expansion_verified: bool = False
    structure_present: bool = False
    structure_verified: bool = False
    blocking_questions: int = 0
    detail_required_missing: int = 0
    structure_approved: bool = False
    work_unit_ready: bool = False
    work_unit_applied: bool = False
    next_work_unit_ready: bool = False
    project_complete: bool = False
    structure_revision_required: bool = False

    def __post_init__(self) -> None:
        if self.blocking_questions < 0:
            raise GuidedTransitionError("blocking_questions는 0 이상이어야 합니다.")
        if self.detail_required_missing < 0:
            raise GuidedTransitionError("detail_required_missing은 0 이상이어야 합니다.")


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "WELCOME": {"IDEA_CAPTURE"},
    "IDEA_CAPTURE": {"IDEA_EXPANSION_READY", "WELCOME"},
    "IDEA_EXPANSION_READY": {"EXPANSION_REVIEW", "IDEA_CAPTURE"},
    "EXPANSION_REVIEW": {"QUESTION_ROUND", "STRUCTURE_MAP_READY", "IDEA_EXPANSION_READY"},
    "QUESTION_ROUND": {"STRUCTURE_MAP_READY", "EXPANSION_REVIEW"},
    "STRUCTURE_MAP_READY": {"STRUCTURE_REVIEW", "QUESTION_ROUND"},
    "STRUCTURE_REVIEW": {"STRUCTURE_APPROVED", "QUESTION_ROUND", "STRUCTURE_MAP_READY"},
    "STRUCTURE_APPROVED": {"FIRST_WORK_UNIT", "STRUCTURE_REVIEW"},
    "FIRST_WORK_UNIT": {"WORK_UNIT_REVIEW", "STRUCTURE_REVIEW"},
    "WORK_UNIT_REVIEW": {"NEXT_WORK_UNIT", "PROJECT_READY", "STRUCTURE_REVIEW"},
    "NEXT_WORK_UNIT": {"WORK_UNIT_REVIEW", "PROJECT_READY", "STRUCTURE_REVIEW"},
    "PROJECT_READY": {"NEXT_WORK_UNIT", "STRUCTURE_REVIEW"},
}


def _stage(value: str | GuidedStage) -> str:
    result = value.value if isinstance(value, GuidedStage) else str(value).strip()
    if result not in GUIDED_STAGES:
        raise GuidedTransitionError(f"지원하지 않는 Guided stage입니다: {result}")
    return result


def _guard(current: str, target: str, context: TransitionContext) -> None:
    if current == "WELCOME" and target == "IDEA_CAPTURE":
        return
    if current == "IDEA_CAPTURE" and target == "IDEA_EXPANSION_READY":
        if not context.seed_ready:
            raise GuidedTransitionError("IdeaSeed가 검증되기 전에는 분석 준비 단계로 갈 수 없습니다.")
        return
    if current == "IDEA_EXPANSION_READY" and target == "EXPANSION_REVIEW":
        if not context.dispatch_approved:
            raise GuidedTransitionError("명시적 dispatch 승인 없이 모델 분석을 시작할 수 없습니다.")
        if not context.expansion_verified:
            raise GuidedTransitionError("검증된 IdeaExpansion 없이 보강안을 열 수 없습니다.")
        return
    if current == "EXPANSION_REVIEW" and target == "QUESTION_ROUND":
        if not context.expansion_verified or context.blocking_questions == 0:
            raise GuidedTransitionError("blocking 질문이 있는 검증된 보강안에서만 질문 단계로 갈 수 있습니다.")
        return
    if current in {"EXPANSION_REVIEW", "QUESTION_ROUND"} and target == "STRUCTURE_MAP_READY":
        if context.blocking_questions:
            raise GuidedTransitionError("blocking 질문에 답하기 전에는 구조를 확정할 수 없습니다.")
        if not context.structure_present or not context.structure_verified:
            raise GuidedTransitionError("검증된 StructureMap 없이 구조 준비 단계로 갈 수 없습니다.")
        return
    if current == "STRUCTURE_MAP_READY" and target == "STRUCTURE_REVIEW":
        if not context.structure_present or not context.structure_verified:
            raise GuidedTransitionError("검증된 StructureMap만 검토 단계로 열 수 있습니다.")
        return
    if current == "STRUCTURE_REVIEW" and target == "STRUCTURE_MAP_READY":
        if not context.structure_revision_required:
            raise GuidedTransitionError("명시적 구조 개정 없이 구조 준비 단계로 되돌아갈 수 없습니다.")
        if context.blocking_questions:
            raise GuidedTransitionError("blocking 질문에 답하기 전에는 개정 구조를 검토할 수 없습니다.")
        if not context.structure_present or not context.structure_verified:
            raise GuidedTransitionError("검증된 개정 StructureMap 없이 구조 준비 단계로 갈 수 없습니다.")
        return
    if current == "STRUCTURE_REVIEW" and target == "STRUCTURE_APPROVED":
        assert_structure_approval(context)
        if context.detail_required_missing:
            raise GuidedTransitionError("필수 detail slot을 확정하기 전에는 구조를 승인할 수 없습니다.")
        return
    if current == "STRUCTURE_APPROVED" and target == "FIRST_WORK_UNIT":
        if not context.structure_approved or not context.work_unit_ready:
            raise GuidedTransitionError("승인된 구조와 준비된 첫 WorkUnit이 필요합니다.")
        return
    if current == "FIRST_WORK_UNIT" and target == "WORK_UNIT_REVIEW":
        if not context.work_unit_ready:
            raise GuidedTransitionError("준비된 WorkUnit 없이 작업 검토를 열 수 없습니다.")
        return
    if current in {"WORK_UNIT_REVIEW", "NEXT_WORK_UNIT"} and target == "NEXT_WORK_UNIT":
        if not context.work_unit_applied or context.structure_revision_required:
            raise GuidedTransitionError("현재 WorkUnit 적용 또는 구조 재검토가 끝나야 다음 작업으로 갈 수 있습니다.")
        if not context.next_work_unit_ready:
            raise GuidedTransitionError("다음 WorkUnit이 준비되지 않았습니다.")
        return
    if current in {"WORK_UNIT_REVIEW", "NEXT_WORK_UNIT"} and target == "PROJECT_READY":
        if not context.work_unit_applied or not context.project_complete:
            raise GuidedTransitionError("모든 필요한 WorkUnit 적용이 끝나야 프로젝트 완료로 갈 수 있습니다.")
        return
    if target == "STRUCTURE_REVIEW" and context.structure_revision_required:
        if not context.structure_present or not context.structure_verified:
            raise GuidedTransitionError("재검토할 검증된 StructureMap이 필요합니다.")
        return
    if current == "STRUCTURE_REVIEW" and target == "QUESTION_ROUND":
        if context.blocking_questions == 0:
            raise GuidedTransitionError("blocking 질문이 없으면 질문 라운드로 되돌아갈 수 없습니다.")
        return
    if current == "STRUCTURE_MAP_READY" and target == "QUESTION_ROUND":
        if context.blocking_questions == 0:
            raise GuidedTransitionError("blocking 질문이 없으면 질문 라운드로 되돌아갈 수 없습니다.")
        return
    if current == "PROJECT_READY" and target == "NEXT_WORK_UNIT":
        if not context.next_work_unit_ready:
            raise GuidedTransitionError("재개할 다음 WorkUnit이 준비되지 않았습니다.")
        return
    # Backward navigation to edit an earlier proposal is intentionally narrow.
    if target in {"WELCOME", "IDEA_CAPTURE", "IDEA_EXPANSION_READY"}:
        return
    raise GuidedTransitionError(f"현재 상태에서 허용되지 않는 전이입니다: {current} → {target}")


def transition_stage(
    current: str | GuidedStage,
    target: str | GuidedStage,
    *,
    context: TransitionContext | None = None,
) -> str:
    """Validate one explicit transition; non-adjacent stage skips are rejected."""

    current_stage = _stage(current)
    target_stage = _stage(target)
    if current_stage == target_stage:
        return current_stage
    if target_stage not in _ALLOWED_TRANSITIONS[current_stage]:
        raise GuidedTransitionError(f"단계 건너뛰기는 허용되지 않습니다: {current_stage} → {target_stage}")
    _guard(current_stage, target_stage, context or TransitionContext())
    return target_stage


def assert_structure_approval(context: TransitionContext) -> None:
    """Require every deterministic approval fact before exposing approval."""

    if not context.structure_present or not context.structure_verified:
        raise GuidedTransitionError("검증된 StructureMap 없이 승인할 수 없습니다.")
    if context.blocking_questions:
        raise GuidedTransitionError("blocking 질문이 남아 있어 구조를 승인할 수 없습니다.")


def assert_seed_unchanged(original: IdeaSeed, candidate: IdeaSeed) -> None:
    """Reject any attempt to replace the original seed in-place."""

    if original.raw_hash != candidate.raw_hash or original.raw_text.encode("utf-8") != candidate.raw_text.encode("utf-8"):
        raise GuidedTransitionError("IdeaSeed 원문은 revision 중에 변경할 수 없습니다.")


def transition_session(
    session: GuidedProjectSession,
    target: str | GuidedStage,
    *,
    context: TransitionContext | None = None,
) -> GuidedProjectSession:
    """Return a new draft snapshot after a validated stage transition."""

    stage = transition_stage(session.stage, target, context=context)
    return replace(session, stage=stage, updated_at=utc_now())


__all__ = [
    "GuidedStage",
    "GuidedTransitionError",
    "TransitionContext",
    "assert_seed_unchanged",
    "assert_structure_approval",
    "transition_session",
    "transition_stage",
]
