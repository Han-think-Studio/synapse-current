"""Deterministic artifact manifest generation from an idea blueprint."""

from dataclasses import dataclass

from synapse.core.idea_blueprint import IdeaBlueprint
from synapse.core.idea_profiles import ZONE_SPECS, artifact_family_for_zone
from synapse.core.idea_reactive_depth import reactive_requirements_for
from synapse.core.idea_session import canonical_hash


@dataclass(frozen=True, slots=True)
class ValidationCase:
    kind: str
    scenario: str
    expected: str
    on_failure: str


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    source_zone: str
    source_id: str
    target_zone: str
    target_id: str
    kind: str

    def to_record(self) -> dict[str, str]:
        return {
            "source_zone": self.source_zone,
            "source_id": self.source_id,
            "target_zone": self.target_zone,
            "target_id": self.target_id,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class ArtifactProposal:
    artifact_id: str
    zone: str
    path: str
    purpose: str
    required: bool
    validation_cases: tuple[str, ...]
    seed_hash: str
    blueprint_id: str
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    validation_specs: tuple[ValidationCase, ...]
    cross_refs: tuple[ArtifactReference, ...]
    required_output_fields: tuple[str, ...]
    reactive_requirements: tuple[str, ...]
    authored_content: str = ""

    @property
    def basis_hash(self) -> str:
        """Hash what this artifact inherits: the idea, the blueprint, the contract.

        An artifact is never independent of the idea it realizes -- independence would
        let the parts drift apart silently, which is the disconnection this structure
        exists to prevent. So the root travels with every artifact, in this layer.
        """

        return canonical_hash(
            {
                "artifact_id": self.artifact_id,
                "zone": self.zone,
                "path": self.path,
                "purpose": self.purpose,
                "required": self.required,
                "seed_hash": self.seed_hash,
                "blueprint_id": self.blueprint_id,
                "depends_on": list(self.depends_on),
                "acceptance_criteria": list(self.acceptance_criteria),
                "validation_specs": [
                    {
                        "kind": item.kind,
                        "scenario": item.scenario,
                        "expected": item.expected,
                        "on_failure": item.on_failure,
                    }
                    for item in self.validation_specs
                ],
                "cross_refs": [item.to_record() for item in self.cross_refs],
                "required_output_fields": list(self.required_output_fields),
                "reactive_requirements": list(self.reactive_requirements),
            }
        )

    @property
    def authored_hash(self) -> str:
        """Hash what was actually written here, separately from what it inherits."""

        return canonical_hash({"authored_content": self.authored_content})

    @property
    def content_hash(self) -> str:
        """Combine both layers, so a change can still be detected as one value.

        Keeping the layers addressable is what lets a later comparison say *why*
        something is stale -- the root moved, someone wrote here, or both -- instead
        of only that it moved.
        """

        return canonical_hash({"basis": self.basis_hash, "authored": self.authored_hash})

    def to_record(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "zone": self.zone,
            "path": self.path,
            "purpose": self.purpose,
            "required": self.required,
            "seed_hash": self.seed_hash,
            "blueprint_id": self.blueprint_id,
            "depends_on": list(self.depends_on),
            "acceptance_criteria": list(self.acceptance_criteria),
            "validation_cases": [
                {
                    "kind": case.kind,
                    "scenario": case.scenario,
                    "expected": case.expected,
                    "on_failure": case.on_failure,
                }
                for case in self.validation_specs
            ],
            "cross_refs": [reference.to_record() for reference in self.cross_refs],
            "required_output_fields": list(self.required_output_fields),
            "reactive_requirements": list(self.reactive_requirements),
            "basis_hash": self.basis_hash,
            "authored_hash": self.authored_hash,
            "content_hash": self.content_hash,
        }


def _path_for(profile_id: str, zone: str, family: str) -> str:
    safe_profile = profile_id.replace("-", "_")
    safe_family = family.replace(" ", "_")
    return f"{safe_profile}/05_zones/{zone}/{safe_family}.md"


def build_artifact_manifest(blueprint: IdeaBlueprint) -> tuple[ArtifactProposal, ...]:
    """Return proposed files for all nine zones without touching the filesystem."""

    if not isinstance(blueprint, IdeaBlueprint):
        raise TypeError("blueprint must be an IdeaBlueprint")
    proposals: list[ArtifactProposal] = []
    specs = {spec.zone: spec for spec in ZONE_SPECS}
    for zone in blueprint.zones:
        family = artifact_family_for_zone(blueprint.profile_id, zone.zone)
        artifact_id = f"{blueprint.profile_id}.{zone.zone}.{family}.v1"
        spec = specs[zone.zone]
        validation_specs = (
            ValidationCase("normal", "기본 입력", "기대 결과가 생성됨", "원인 기록 후 수정"),
            ValidationCase("boundary", "빈 값·최대 범위·권한 경계", "경계가 명시적으로 처리됨", "안전한 거부 또는 제한"),
            ValidationCase("failure", "실패·타임아웃·불일치", "실패 상태와 복구 경로가 기록됨", "중단하고 승인 대기"),
        )
        cross_refs = tuple(
            ArtifactReference(
                source_zone=dependency,
                source_id=f"{blueprint.profile_id}.{dependency}",
                target_zone=zone.zone,
                target_id=artifact_id,
                kind="depends_on",
            )
            for dependency in spec.required_inputs
        )
        proposals.append(
            ArtifactProposal(
                artifact_id=artifact_id,
                zone=zone.zone,
                path=_path_for(blueprint.profile_id, zone.zone, family),
                purpose=f"{zone.zone} 구역의 {family} 제안 산출물",
                required=True,
                validation_cases=(
                    "normal: 기본 입력과 기대 결과가 연결됩니다.",
                    "boundary: 빈 값·최대 범위·권한 경계를 확인합니다.",
                    "failure: 실패 원인과 복구 또는 중단 조건을 기록합니다.",
                ),
                seed_hash=blueprint.seed_hash,
                blueprint_id=blueprint.id,
                depends_on=spec.required_inputs,
                acceptance_criteria=(spec.completion_gate, "원본 seed와 blueprint를 역추적할 수 있음"),
                validation_specs=validation_specs,
                cross_refs=cross_refs,
                required_output_fields=spec.required_outputs,
                reactive_requirements=reactive_requirements_for(blueprint.profile_id, zone.zone),
            )
        )
    return tuple(proposals)


__all__ = ["ArtifactProposal", "ArtifactReference", "ValidationCase", "build_artifact_manifest"]
