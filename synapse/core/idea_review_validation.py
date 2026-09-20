"""Read-only integrity checks for idea review packets."""

from typing import Any


def validate_idea_review_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic validation evidence without mutating the packet."""

    if not isinstance(packet, dict):
        raise TypeError("review packet must be a dictionary")
    blueprint = packet.get("blueprint")
    artifacts = packet.get("artifacts")
    quality = packet.get("quality")
    approval = packet.get("approval")
    errors: list[str] = []
    if not isinstance(blueprint, dict):
        errors.append("blueprint_missing")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifacts_missing")
    if not isinstance(quality, dict):
        errors.append("quality_missing")
    if not isinstance(approval, dict) or approval.get("required") is not True:
        errors.append("approval_gate_missing")
    if isinstance(blueprint, dict) and isinstance(artifacts, list):
        seed_hash = blueprint.get("seed_hash")
        blueprint_id = blueprint.get("id")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                errors.append("artifact_not_object")
                continue
            if artifact.get("seed_hash") != seed_hash:
                errors.append("artifact_seed_mismatch")
            if artifact.get("blueprint_id") != blueprint_id:
                errors.append("artifact_blueprint_mismatch")
    valid = not errors
    return {
        "schema_version": "idea.review.validation.v1",
        "valid": valid,
        "status": "READY_FOR_APPROVAL" if valid else "BLOCKED",
        "errors": sorted(set(errors)),
        "mutation": False,
    }


__all__ = ["validate_idea_review_packet"]
