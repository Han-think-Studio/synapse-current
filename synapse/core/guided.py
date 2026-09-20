"""Deterministic, read-only Guided Build plans.

Guided Build is the review boundary between intake evidence and any later
export, executor, or Proposal bridge. It explains the next safe actions but
does not stage files, call a model, or mutate Canonical State.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from synapse.core.cognitive import CognitiveFrame, build_cognitive_frame
from synapse.core.failure import FailureReport, triage_frame
from synapse.core.ir import SynapseIR

_EXPORT_LEVELS = {"ideas_only", "reviewed", "full"}
_STEP_STATUS = {"BLOCKED", "REVIEW", "READY"}


@dataclass(frozen=True, slots=True)
class BuildStep:
    id: str
    kind: str
    status: str
    subject_ids: tuple[str, ...]
    reason: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.kind.strip() or self.status not in _STEP_STATUS:
            raise ValueError("BuildStep에는 id, kind, 유효한 status가 필요합니다.")
        if not self.reason.strip():
            raise ValueError("BuildStep reason이 비어 있습니다.")
        object.__setattr__(self, "subject_ids", tuple(sorted(set(self.subject_ids))))
        object.__setattr__(self, "depends_on", tuple(sorted(set(self.depends_on))))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "subject_ids": list(self.subject_ids),
            "reason": self.reason,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True, slots=True)
class GuidedBuildPlan:
    id: str
    frame_id: str
    export_level: str
    steps: tuple[BuildStep, ...]
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.frame_id.strip():
            raise ValueError("GuidedBuildPlan id와 frame_id가 필요합니다.")
        if self.export_level not in _EXPORT_LEVELS:
            raise ValueError(f"지원하지 않는 export_level입니다: {self.export_level}")
        steps = tuple(self.steps)
        ids = [step.id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("GuidedBuildPlan step id가 중복됩니다.")
        known = set(ids)
        missing_dependencies = sorted(
            dependency
            for step in steps
            for dependency in step.depends_on
            if dependency not in known
        )
        if missing_dependencies:
            raise ValueError(f"GuidedBuildPlan dependency가 없습니다: {missing_dependencies}")
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def status(self) -> str:
        statuses = {step.status for step in self.steps}
        if "BLOCKED" in statuses:
            return "BLOCKED"
        if "REVIEW" in statuses:
            return "REVIEW"
        return "READY"

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "frame_id": self.frame_id,
            "export_level": self.export_level,
            "status": self.status,
            "steps": [step.to_record() for step in self.steps],
            "metadata": dict(self.metadata),
        }


def build_guided_plan(
    ir: SynapseIR,
    *,
    frame: CognitiveFrame | None = None,
    failures: FailureReport | None = None,
    export_level: str = "reviewed",
) -> GuidedBuildPlan:
    """Build a reproducible review plan without changing any input object."""
    ir.validate(strict=True)
    if export_level not in _EXPORT_LEVELS:
        raise ValueError(f"지원하지 않는 export_level입니다: {export_level}")

    expected_frame = build_cognitive_frame(ir)
    selected_frame = frame or expected_frame
    if selected_frame.id != expected_frame.id or selected_frame.subject_ids != ir.node_ids:
        raise ValueError("IR과 Cognitive Frame이 일치하지 않습니다.")

    expected_failures = triage_frame(selected_frame)
    selected_failures = failures or expected_failures
    if selected_failures.to_record() != expected_failures.to_record():
        raise ValueError("Cognitive Frame과 Failure Report가 일치하지 않습니다.")

    steps: list[BuildStep] = [
        BuildStep(
            id="verify.frame",
            kind="verify",
            status="READY",
            subject_ids=selected_frame.subject_ids,
            reason="IR validation, premise partition, and evidence frame are reproducible.",
        )
    ]
    review_step_ids: list[str] = []
    for cluster in selected_failures.clusters:
        step_id = f"review.{cluster.key}"
        review_step_ids.append(step_id)
        steps.append(
            BuildStep(
                id=step_id,
                kind="review",
                status="BLOCKED" if cluster.severity == "BLOCKER" else "REVIEW",
                subject_ids=cluster.subject_ids,
                reason=cluster.recommendation,
                depends_on=("verify.frame",),
            )
        )

    steps.append(
        BuildStep(
            id="choose.export_scope",
            kind="approval",
            status="REVIEW",
            subject_ids=(),
            reason=f"Owner must approve the '{export_level}' handoff scope before staging.",
            depends_on=("verify.frame",),
        )
    )
    staging_status = "BLOCKED" if selected_failures.has_blockers else (
        "REVIEW" if selected_failures.clusters else "READY"
    )
    steps.append(
        BuildStep(
            id="stage.workspace",
            kind="stage",
            status=staging_status,
            subject_ids=selected_frame.subject_ids,
            reason=(
                "Resolve blocker clusters before staging."
                if selected_failures.has_blockers
                else "Stage only after review clusters and export scope are acknowledged."
                if selected_failures.clusters
                else "Stage the reviewed workspace after the owner approves export scope."
            ),
            depends_on=(*review_step_ids, "choose.export_scope"),
        )
    )

    canonical = json.dumps(
        {
            "frame_id": selected_frame.id,
            "failures": selected_failures.to_record(),
            "export_level": export_level,
            "steps": [step.to_record() for step in steps],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    plan_id = f"build:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return GuidedBuildPlan(
        id=plan_id,
        frame_id=selected_frame.id,
        export_level=export_level,
        steps=tuple(steps),
        metadata={
            "ir_version": ir.version,
            "subject_count": len(ir.nodes),
            "failure_cluster_count": len(selected_failures.clusters),
            "canonical_mutation": False,
        },
    )


__all__ = ["BuildStep", "GuidedBuildPlan", "build_guided_plan"]
