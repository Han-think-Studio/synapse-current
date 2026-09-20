"""Explicit human approval receipt for creative proposals; no apply side effects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from synapse.core.idea_session import canonical_hash


class CreativeApprovalError(ValueError):
    """Raised when a creative proposal cannot receive a fresh approval receipt."""


def build_creative_approval_receipt(
    proposal: Mapping[str, Any], *, reviewer: str, approved_at: str, current: Mapping[str, Any]
) -> dict[str, Any]:
    """Create a deterministic approval receipt after rechecking freshness."""

    if not isinstance(proposal, Mapping) or not isinstance(current, Mapping):
        raise CreativeApprovalError("proposal and current must be objects")
    reviewer = str(reviewer).strip()
    approved_at = str(approved_at).strip()
    if not reviewer or not approved_at:
        raise CreativeApprovalError("reviewer and approved_at are required")
    if proposal.get("status") != "READY_FOR_REVIEW":
        raise CreativeApprovalError("only READY_FOR_REVIEW proposals can be approved")
    verification = proposal.get("verification")
    if not isinstance(verification, Mapping) or verification.get("passed") is not True:
        raise CreativeApprovalError("creative verification must pass before approval")
    receipt = proposal.get("quality_receipt")
    if not isinstance(receipt, Mapping):
        raise CreativeApprovalError("quality_receipt is required")
    required = ("seed_hash", "map_id", "creative_map_hash", "structure_map_hash", "receipt_hash", "revision", "workspace_base_hash", "artifact_scope", "file_base_hashes")
    for key in required:
        if proposal.get(key) != current.get(key):
            raise CreativeApprovalError(f"stale creative proposal: {key}")
    if receipt.get("receipt_hash") != proposal.get("receipt_hash"):
        raise CreativeApprovalError("quality receipt hash mismatch")
    scope = proposal.get("artifact_scope")
    file_hashes = proposal.get("file_base_hashes")
    if not isinstance(scope, list) or not scope or any(not isinstance(item, str) or not item.strip() for item in scope):
        raise CreativeApprovalError("artifact_scope must be a non-empty string array")
    if not isinstance(file_hashes, Mapping) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in file_hashes.items()):
        raise CreativeApprovalError("file_base_hashes must be a string mapping")
    identity = {"seed_hash": proposal["seed_hash"], "map_id": proposal["map_id"], "creative_map_hash": proposal["creative_map_hash"],
                "structure_map_hash": proposal["structure_map_hash"], "receipt_hash": proposal["receipt_hash"],
                "revision": proposal["revision"], "workspace_base_hash": proposal["workspace_base_hash"],
                "artifact_scope": sorted(set(scope)), "file_base_hashes": dict(sorted(file_hashes.items())),
                "reviewer": reviewer, "approved_at": approved_at}
    approval_id = f"creative-approval:{canonical_hash(identity).removeprefix('sha256:')}"
    return {"schema_version": "creative.approval.v1", "approval_id": approval_id,
            **identity, "status": "APPROVED", "canonical_mutation": False,
            "execution_allowed": False}


__all__ = ["CreativeApprovalError", "build_creative_approval_receipt"]
