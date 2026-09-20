"""Read-only pre-apply validation for creative approval receipts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from synapse.core.creative_stale import compare_workspace_snapshot
from synapse.core.idea_session import canonical_hash
from synapse.core.workspace import WorkspaceError, _normalise_relative_path


def validate_creative_preapply(
    approval: Mapping[str, Any], *, approved_snapshot: Mapping[str, Any], current_snapshot: Mapping[str, Any], proposals: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """Fail closed if approval identity or workspace snapshot is stale."""

    errors: list[str] = []
    if not isinstance(approval, Mapping):
        return {"passed": False, "errors": ["approval_invalid"], "canonical_mutation": False, "filesystem_mutation": False, "provider_called": False, "execution_allowed": False}
    if not isinstance(approved_snapshot, Mapping) or not isinstance(current_snapshot, Mapping):
        return {"passed": False, "errors": ["workspace_snapshot_invalid"], "canonical_mutation": False, "filesystem_mutation": False, "provider_called": False, "execution_allowed": False}
    if not isinstance(approved_snapshot.get("files", []), list) or not isinstance(current_snapshot.get("files", []), list):
        return {"passed": False, "errors": ["workspace_snapshot_files_invalid"], "canonical_mutation": False, "filesystem_mutation": False, "provider_called": False, "execution_allowed": False}
    if approval.get("status") != "APPROVED":
        errors.append("approval_not_approved")
    for key in ("approval_id", "seed_hash", "map_id", "creative_map_hash", "structure_map_hash", "receipt_hash", "revision"):
        if not approval.get(key):
            errors.append(f"approval_field_missing:{key}")
    approved_files = {str(item.get("path")): item.get("sha256") for item in approved_snapshot.get("files", []) if isinstance(item, Mapping)}
    bound_files = approval.get("file_base_hashes")
    if not isinstance(bound_files, Mapping) or {
        path: str(value).removeprefix("sha256:") for path, value in bound_files.items()
    } != {
        path: str(value).removeprefix("sha256:") for path, value in approved_files.items()
    }:
        errors.append("approval_file_hash_binding_mismatch")
    expected_workspace_hash = approval.get("workspace_base_hash")
    approved_hash_payload = {path: f"sha256:{str(value).removeprefix('sha256:')}" for path, value in sorted(approved_files.items())}
    if expected_workspace_hash and expected_workspace_hash != canonical_hash(approved_hash_payload):
        errors.append("approval_workspace_hash_binding_mismatch")
    seen_paths: set[str] = set()
    workspace_roots: set[str] = set()
    approved_root = str(approved_snapshot.get("workspace_path", "")).strip()
    current_root = str(current_snapshot.get("workspace_path", "")).strip()
    if approved_root and current_root and approved_root != current_root:
        errors.append("workspace_root_mismatch")
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            errors.append("proposal_invalid")
            continue
        path = str(proposal.get("path", ""))
        if not path or path in seen_paths:
            errors.append(f"proposal_duplicate_or_missing_path:{path}")
        seen_paths.add(path)
        try:
            normalized_path = _normalise_relative_path(path)
            if normalized_path != path:
                errors.append(f"proposal_path_not_normalized:{path}")
        except (WorkspaceError, TypeError):
            errors.append(f"proposal_path_invalid:{path}")
        status = proposal.get("status")
        if status != "PROPOSED":
            errors.append(f"proposal_status_invalid:{path}")
        if proposal.get("canonical_mutation") is True:
            errors.append(f"proposal_canonical_mutation:{path}")
        if proposal.get("workspace_path"):
            workspace_roots.add(str(proposal["workspace_path"]))
        content = proposal.get("content")
        content_hash = proposal.get("content_sha256")
        if not isinstance(content, str) or not isinstance(content_hash, str) or hashlib.sha256(content.encode("utf-8")).hexdigest() != content_hash.removeprefix("sha256:"):
            errors.append(f"proposal_content_hash_mismatch:{path}")
        base = proposal.get("base_sha256")
        bound = bound_files.get(path) if isinstance(bound_files, Mapping) else None
        if bound is None or (base is not None and str(base).removeprefix("sha256:") != str(bound).removeprefix("sha256:")):
            errors.append(f"proposal_base_hash_mismatch:{path}")
    if len(workspace_roots) > 1:
        errors.append("proposal_workspace_root_mismatch")
    if workspace_roots and (approved_root or current_root) and any(root not in {approved_root, current_root} for root in workspace_roots):
        errors.append("proposal_workspace_root_mismatch")
    comparison = compare_workspace_snapshot(approved_snapshot, current_snapshot)
    if not comparison["passed"]:
        errors.append("workspace_snapshot_changed")
    errors = sorted(set(errors))
    return {
        "passed": not errors,
        "errors": errors,
        "workspace": comparison,
        "canonical_mutation": False,
        "filesystem_mutation": False,
        "provider_called": False,
        "execution_allowed": False,
    }


__all__ = ["validate_creative_preapply"]
