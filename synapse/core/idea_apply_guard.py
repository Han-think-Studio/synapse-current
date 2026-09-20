"""Read-only guard deciding whether a review packet may proceed to apply."""

from typing import Any


def evaluate_idea_apply_guard(packet: dict[str, Any], *, approved: bool = False) -> dict[str, Any]:
    """Return an apply decision; this function never applies files or commands."""

    if not isinstance(packet, dict):
        raise TypeError("review packet must be a dictionary")
    quality = packet.get("quality", {})
    integrity = packet.get("integrity", {})
    reasons: list[str] = []
    if quality.get("status") != "READY_FOR_REVIEW":
        reasons.append("quality_not_ready")
    if integrity.get("status") != "READY_FOR_APPROVAL":
        reasons.append("integrity_not_ready")
    if approved is not True:
        reasons.append("explicit_approval_missing")
    allowed = not reasons
    return {
        "schema_version": "idea.apply.guard.v1",
        "allowed": allowed,
        "status": "APPROVED_FOR_APPLY" if allowed else "BLOCKED",
        "reasons": reasons,
        "filesystem_mutation": False,
        "external_execution": False,
    }


__all__ = ["evaluate_idea_apply_guard"]
