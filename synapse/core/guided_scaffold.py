"""Phase 28D approved-structure to scaffold preview compiler.

This module only compiles a verified, explicitly approved ``StructureMap``
into an in-memory preview.  It never creates files, applies a Proposal, runs a
WorkUnit, calls a model, or mutates the Canonical Registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.idea_session import IdeaSeed, canonical_hash
from synapse.core.scaffold import ScaffoldError, build_scaffold_plan
from synapse.core.structure_map import ARTIFACT_ROLE_PATHS, StructureMapProposal


class GuidedScaffoldError(ValueError):
    """Raised when a StructureMap cannot cross the Phase 28D preview boundary."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ScaffoldCandidateFile:
    """One deterministic logical-role to file candidate."""

    path: str
    role: str
    title: str
    purpose: str
    source_node_ids: tuple[str, ...] = ()
    source_work_unit_ids: tuple[str, ...] = ()
    required: bool = False
    in_preset: bool = False

    def __post_init__(self) -> None:
        if not self.path.strip() or self.path.startswith("/") or ".." in self.path.split("/"):
            raise GuidedScaffoldError(f"안전하지 않은 scaffold 후보 경로입니다: {self.path}")
        if self.role not in ARTIFACT_ROLE_PATHS:
            raise GuidedScaffoldError(f"알 수 없는 artifact_role입니다: {self.role}")
        if not self.title.strip() or not self.purpose.strip():
            raise GuidedScaffoldError("scaffold 후보에는 title과 purpose가 필요합니다.")
        object.__setattr__(self, "source_node_ids", tuple(dict.fromkeys(self.source_node_ids)))
        object.__setattr__(self, "source_work_unit_ids", tuple(dict.fromkeys(self.source_work_unit_ids)))

    @property
    def status(self) -> str:
        return "MATCHED" if self.in_preset else "ROLE_NOT_IN_PRESET"

    def to_record(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "title": self.title,
            "purpose": self.purpose,
            "source_node_ids": list(self.source_node_ids),
            "source_work_unit_ids": list(self.source_work_unit_ids),
            "required": self.required,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedScaffoldPreview:
    """A PROPOSED, memory-only scaffold compilation result."""

    id: str
    seed_hash: str
    structure_id: str
    structure_revision: int
    preset_id: str
    project_name: str
    files: tuple[ScaffoldCandidateFile, ...]
    preset_files: tuple[str, ...]
    first_work_unit: Mapping[str, Any] | None
    comparison: Mapping[str, Any]
    metadata: Mapping[str, Any]
    status: str = "PROPOSED"
    canonical_mutation: bool = False
    filesystem_mutation: bool = False
    work_unit_execution: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.seed_hash.strip() or not self.structure_id.strip():
            raise GuidedScaffoldError("scaffold preview 식별자와 hash가 필요합니다.")
        if self.structure_revision < 1:
            raise GuidedScaffoldError("structure revision은 1 이상이어야 합니다.")
        if self.status != "PROPOSED":
            raise GuidedScaffoldError("scaffold preview는 PROPOSED로 시작해야 합니다.")
        if self.canonical_mutation or self.filesystem_mutation or self.work_unit_execution:
            raise GuidedScaffoldError("Phase 28D preview는 어떠한 mutation도 허용하지 않습니다.")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "preset_files", tuple(dict.fromkeys(self.preset_files)))
        object.__setattr__(self, "comparison", MappingProxyType(dict(self.comparison)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seed_hash": self.seed_hash,
            "structure_id": self.structure_id,
            "structure_revision": self.structure_revision,
            "preset_id": self.preset_id,
            "project_name": self.project_name,
            "status": self.status,
            "files": [item.to_record() for item in self.files],
            "preset_files": list(self.preset_files),
            "comparison": dict(self.comparison),
            "first_work_unit": dict(self.first_work_unit) if self.first_work_unit else None,
            "metadata": dict(self.metadata),
            "canonical_mutation": self.canonical_mutation,
            "filesystem_mutation": self.filesystem_mutation,
            "work_unit_execution": self.work_unit_execution,
        }


def _first_work_unit(structure: StructureMapProposal) -> dict[str, Any] | None:
    if not structure.work_units:
        return None
    unit = structure.work_units[0]
    paths = tuple(sorted({ARTIFACT_ROLE_PATHS[role] for role in unit.artifact_roles}))
    return {
        "id": unit.id,
        "title": unit.title,
        "purpose": unit.purpose,
        "prerequisites": list(unit.prerequisites),
        "source_node_ids": list(unit.source_node_ids),
        "artifact_roles": list(unit.artifact_roles),
        "paths": list(paths),
        "completion_criteria": unit.completion_criteria,
        "downstream_impacts": list(unit.downstream_impacts),
        "status": unit.status,
        "remaining_work_unit_count": max(0, len(structure.work_units) - 1),
        "review_only": True,
        "execution_allowed": False,
    }


def compile_approved_structure_preview(
    seed: IdeaSeed,
    structure: StructureMapProposal,
    *,
    approved_structure_id: str,
    project_name: str = "",
    preset_id: str = "",
) -> GuidedScaffoldPreview:
    """Compile one approved StructureMap into a deterministic preview only."""

    approved_id = str(approved_structure_id).strip()
    if not approved_id or approved_id != structure.id:
        raise GuidedScaffoldError("승인된 StructureMap만 scaffold preview로 컴파일할 수 있습니다.")
    if structure.seed_hash != seed.raw_hash:
        raise GuidedScaffoldError("StructureMap seed hash가 현재 IdeaSeed와 다릅니다.")
    if structure.blocking_questions:
        raise GuidedScaffoldError("blocking 질문이 남아 있는 StructureMap은 컴파일할 수 없습니다.")
    selected_preset = str(preset_id).strip() or seed.preset_hint
    selected_name = str(project_name).strip() or seed.project_name
    try:
        preset_plan = build_scaffold_plan(
            seed.raw_text,
            preset_id=selected_preset,
            project_name=selected_name,
        )
    except ScaffoldError as exc:
        raise GuidedScaffoldError(str(exc)) from exc

    preset_paths = tuple(sorted(item.path for item in preset_plan.files))
    preset_path_set = set(preset_paths)
    by_role: dict[str, dict[str, Any]] = {}
    for node in structure.nodes:
        if node.artifact_role is None:
            continue
        entry = by_role.setdefault(
            node.artifact_role,
            {"titles": [], "purposes": [], "node_ids": [], "work_unit_ids": [], "required": False},
        )
        entry["titles"].append(node.title)
        entry["purposes"].append(node.purpose)
        entry["node_ids"].append(node.id)
        entry["required"] = entry["required"] or node.required
    for unit in structure.work_units:
        for role in unit.artifact_roles:
            entry = by_role.setdefault(
                role,
                {"titles": [], "purposes": [], "node_ids": [], "work_unit_ids": [], "required": False},
            )
            entry["titles"].append(unit.title)
            entry["purposes"].append(unit.purpose)
            entry["work_unit_ids"].append(unit.id)

    files: list[ScaffoldCandidateFile] = []
    for role in sorted(by_role):
        entry = by_role[role]
        files.append(
            ScaffoldCandidateFile(
                path=ARTIFACT_ROLE_PATHS[role],
                role=role,
                title=" / ".join(dict.fromkeys(entry["titles"])),
                purpose=" / ".join(dict.fromkeys(entry["purposes"])),
                source_node_ids=tuple(entry["node_ids"]),
                source_work_unit_ids=tuple(entry["work_unit_ids"]),
                required=bool(entry["required"]),
                in_preset=ARTIFACT_ROLE_PATHS[role] in preset_path_set,
            )
        )
    candidate_paths = tuple(sorted(item.path for item in files))
    comparison = {
        "preset_paths": list(preset_paths),
        "candidate_paths": list(candidate_paths),
        "matched_paths": sorted(set(candidate_paths) & preset_path_set),
        "candidate_not_in_preset": sorted(set(candidate_paths) - preset_path_set),
        "preset_only_paths": sorted(preset_path_set - set(candidate_paths)),
    }
    identity = {
        "seed_hash": seed.raw_hash,
        "structure_id": structure.id,
        "structure_revision": structure.revision,
        "map_hash": structure.map_hash,
        "preset_id": selected_preset,
        "project_name": selected_name,
    }
    preview_id = f"guided-scaffold:{canonical_hash(identity)}"
    return GuidedScaffoldPreview(
        id=preview_id,
        seed_hash=seed.raw_hash,
        structure_id=structure.id,
        structure_revision=structure.revision,
        preset_id=selected_preset,
        project_name=selected_name,
        files=tuple(files),
        preset_files=preset_paths,
        first_work_unit=_first_work_unit(structure),
        comparison=comparison,
        metadata={
            "version": 1,
            "llm_invoked": False,
            "compiler": "phase28d.guided_scaffold.v1",
            "approved_structure_id": approved_id,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "work_unit_execution": False,
        },
    )


__all__ = [
    "GuidedScaffoldError",
    "GuidedScaffoldPreview",
    "ScaffoldCandidateFile",
    "compile_approved_structure_preview",
]
