"""Deterministic project continuation blueprint generation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from synapse.core.cognitive import build_cognitive_frame
from synapse.core.guided import GuidedBuildPlan
from synapse.core.ir import SynapseIR


class BlueprintError(ValueError):
    """Raised when a project blueprint cannot be safely compiled."""


@dataclass(frozen=True, slots=True)
class BlueprintEntry:
    path: str
    role: str
    source_ids: tuple[str, ...] = ()
    review_required: bool = False

    def __post_init__(self) -> None:
        if not self.path.strip() or self.path.startswith("/") or ".." in self.path.split("/"):
            raise BlueprintError(f"안전하지 않은 blueprint path입니다: {self.path}")
        if not self.role.strip():
            raise BlueprintError("BlueprintEntry role이 비어 있습니다.")
        object.__setattr__(self, "source_ids", tuple(sorted(set(self.source_ids))))

    def to_record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "role": self.role,
            "source_ids": list(self.source_ids),
            "review_required": self.review_required,
        }


@dataclass(frozen=True, slots=True)
class ProjectBlueprint:
    id: str
    project_name: str
    project_slug: str
    plan_id: str
    export_level: str
    entries: tuple[BlueprintEntry, ...]
    review_required: bool

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.project_name.strip() or not self.project_slug.strip():
            raise BlueprintError("ProjectBlueprint 식별자가 비어 있습니다.")
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise BlueprintError("ProjectBlueprint path가 중복됩니다.")
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda entry: entry.path)))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "project_name": self.project_name,
            "project_slug": self.project_slug,
            "plan_id": self.plan_id,
            "export_level": self.export_level,
            "review_required": self.review_required,
            "entries": [entry.to_record() for entry in self.entries],
        }


def _slug(value: str) -> str:
    normalized = re.sub(r"[^0-9a-z가-힣]+", "-", value.strip().lower()).strip("-")
    return normalized or "synapse-project"


def compile_project_blueprint(
    ir: SynapseIR,
    plan: GuidedBuildPlan,
    *,
    project_name: str = "Synapse Project",
) -> ProjectBlueprint:
    """Compile a manifest only; no files are created and no state is mutated."""
    ir.validate(strict=True)
    if plan.status == "BLOCKED":
        raise BlueprintError("BLOCKED Guided Build plan은 blueprint로 컴파일할 수 없습니다.")
    if plan.frame_id != build_cognitive_frame(ir).id:
        raise BlueprintError("Guided Build plan이 현재 IR과 일치하지 않습니다.")
    name = project_name.strip()
    if not name:
        raise BlueprintError("project_name이 비어 있습니다.")

    source_ids = tuple(
        sorted(
            {
                provenance.source_id
                for node in ir.nodes
                for provenance in node.provenance
            }
        )
    )
    entries = (
        BlueprintEntry("AGENTS.md", "working_agreement", review_required=True),
        BlueprintEntry("README.md", "handoff_readme", source_ids=source_ids, review_required=True),
        BlueprintEntry("synapse/build/plan.json", "guided_build_plan", review_required=True),
        BlueprintEntry("synapse/cognitive/frame.json", "cognitive_frame", source_ids=source_ids),
        BlueprintEntry("synapse/failures/report.json", "failure_report", review_required=True),
        BlueprintEntry("synapse/ir/intake.json", "synapse_ir", source_ids=source_ids),
        BlueprintEntry("synapse/decisions/README.md", "decision_ledger_projection", review_required=True),
    )
    canonical = json.dumps(
        {
            "project_name": name,
            "project_slug": _slug(name),
            "plan": plan.to_record(),
            "entries": [entry.to_record() for entry in entries],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProjectBlueprint(
        id=f"blueprint:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        project_name=name,
        project_slug=_slug(name),
        plan_id=plan.id,
        export_level=plan.export_level,
        entries=entries,
        review_required=plan.status == "REVIEW",
    )


__all__ = ["BlueprintEntry", "BlueprintError", "ProjectBlueprint", "compile_project_blueprint"]
