"""Proposal-only review packet for an idea blueprint."""

from typing import Any

from synapse.core.idea_artifacts import build_artifact_manifest
from synapse.core.idea_blueprint import IdeaBlueprint
from synapse.core.idea_quality import evaluate_idea_blueprint
from synapse.core.idea_review_validation import validate_idea_review_packet


def build_idea_review_packet(blueprint: IdeaBlueprint) -> dict[str, Any]:
    """Combine blueprint, artifacts, and quality evidence for human review."""

    if not isinstance(blueprint, IdeaBlueprint):
        raise TypeError("blueprint must be an IdeaBlueprint")
    artifacts = build_artifact_manifest(blueprint)
    quality = evaluate_idea_blueprint(blueprint)
    packet = {
        "schema_version": "idea.review.v1",
        "blueprint": blueprint.to_record(),
        "artifacts": [artifact.to_record() for artifact in artifacts],
        "quality": quality,
        "approval": {
            "required": True,
            "status": "PENDING",
            "filesystem_mutation": False,
            "external_execution": False,
        },
    }
    packet["integrity"] = validate_idea_review_packet(packet)
    return packet


__all__ = ["build_idea_review_packet"]
