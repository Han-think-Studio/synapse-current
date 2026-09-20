"""Deterministic quality receipt for proposal-only idea blueprints."""

from typing import Any

from synapse.core.idea_artifacts import ArtifactProposal, build_artifact_manifest
from synapse.core.idea_blueprint import IdeaBlueprint
from synapse.core.idea_reactive_depth import COMMON_REACTIVE_REQUIREMENTS


def evaluate_idea_blueprint(blueprint: IdeaBlueprint, *, threshold: int = 80) -> dict[str, Any]:
    """Score structural completeness without invoking a model or applying files."""

    if not isinstance(blueprint, IdeaBlueprint):
        raise TypeError("blueprint must be an IdeaBlueprint")
    if not isinstance(threshold, int) or not 0 <= threshold <= 100:
        raise ValueError("threshold must be an integer from 0 to 100")
    artifacts: tuple[ArtifactProposal, ...] = build_artifact_manifest(blueprint)
    zone_count = len(blueprint.zones)
    artifact_count = len(artifacts)
    expected_zones = {zone.zone for zone in blueprint.zones}
    artifact_zones = [item.zone for item in artifacts]
    artifact_zone_set = set(artifact_zones)
    exact_zone_coverage = (
        len(artifact_zones) == zone_count
        and artifact_zone_set == expected_zones
        and len(artifact_zone_set) == len(artifact_zones)
    )
    complete_validation = all(
        {case.kind for case in item.validation_specs} >= {"normal", "boundary", "failure"}
        and all(case.expected.strip() and case.on_failure.strip() for case in item.validation_specs)
        and bool(item.acceptance_criteria)
        and item.required_output_fields
        for item in artifacts
    )
    complete_cross_refs = all(
        item.cross_refs
        and all(
            reference.source_zone.strip()
            and reference.source_id.strip()
            and reference.target_zone == item.zone
            and reference.target_id == item.artifact_id
            and reference.kind.strip()
            for reference in item.cross_refs
        )
        for item in artifacts
    )
    complete_reactive_depth = all(
        set(COMMON_REACTIVE_REQUIREMENTS).issubset(item.reactive_requirements)
        and item.reactive_requirements
        for item in artifacts
    )
    coverage = (
        100
        if exact_zone_coverage
        else round((len(artifact_zone_set & expected_zones) / zone_count) * 100) if zone_count else 0
    )
    validation_score = 100 if complete_validation else 0
    cross_ref_score = 100 if complete_cross_refs else 0
    reactive_score = 100 if complete_reactive_depth else 0
    score = round((coverage + validation_score + cross_ref_score + reactive_score) / 4)
    status = "READY_FOR_REVIEW" if score >= threshold and exact_zone_coverage else "BLOCKED"
    return {
        "schema_version": "idea.quality.v1",
        "blueprint_id": blueprint.id,
        "seed_hash": blueprint.seed_hash,
        "profile_id": blueprint.profile_id,
        "dimensions": {
            "zone_artifact_coverage": coverage,
            "validation_completeness": validation_score,
            "cross_reference_integrity": cross_ref_score,
            "connected_reaction_depth": reactive_score,
        },
        "total": score,
        "threshold": threshold,
        "status": status,
        "evidence": {
            "zone_count": len(blueprint.zones),
            "priority_zone_count": sum(zone.priority for zone in blueprint.zones),
            "required_zone_count": zone_count,
            "artifact_count": artifact_count,
            "artifact_zones": artifact_zones,
            "missing_zones": sorted(expected_zones - artifact_zone_set),
            "duplicate_zones": sorted(zone for zone in artifact_zone_set if artifact_zones.count(zone) > 1),
            "exact_zone_coverage": exact_zone_coverage,
            "complete_cross_refs": complete_cross_refs,
            "complete_reactive_depth": complete_reactive_depth,
        },
        "proposal_only": True,
        "filesystem_mutation": False,
    }


__all__ = ["evaluate_idea_blueprint"]
