"""Fail-closed, read-only freshness checks for future creative apply operations."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structure_map import ARTIFACT_ROLE_PATHS


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def check_creative_approval_freshness(
    approval: Mapping[str, Any], *, workspace_root: str | Path
) -> dict[str, Any]:
    """Compare approval file bindings to the live workspace without mutation."""

    root = Path(workspace_root).resolve()
    if "status" in approval and approval.get("status") != "APPROVED":
        return {"passed": False, "errors": ["invalid_status"], "canonical_mutation": False, "filesystem_mutation": False}
    expected = approval.get("file_base_hashes")
    if not isinstance(expected, Mapping) or not expected:
        return {"passed": False, "errors": ["file_base_hashes_missing"], "canonical_mutation": False, "filesystem_mutation": False}
    scope = approval.get("artifact_scope")
    if scope is not None and not isinstance(scope, list):
        return {"passed": False, "errors": ["artifact_scope_invalid"], "canonical_mutation": False, "filesystem_mutation": False}
    if isinstance(scope, list):
        allowed_paths = {ARTIFACT_ROLE_PATHS.get(str(role), "") for role in scope}
        if any(path not in allowed_paths for path in expected):
            return {"passed": False, "errors": ["artifact_scope_mismatch"], "canonical_mutation": False, "filesystem_mutation": False}
    current: dict[str, str] = {}
    errors: list[str] = []
    for raw_path, expected_hash in sorted(expected.items()):
        candidate = (root / str(raw_path)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            errors.append(f"path_outside_workspace:{raw_path}")
            continue
        if not candidate.is_file():
            errors.append(f"missing_file:{raw_path}")
            continue
        actual = _file_hash(candidate)
        current[str(raw_path)] = actual
        normalized_expected = str(expected_hash).removeprefix("sha256:")
        if actual.removeprefix("sha256:") != normalized_expected:
            errors.append(f"file_hash_mismatch:{raw_path}")
    expected_workspace = approval.get("workspace_base_hash")
    current_workspace = canonical_hash(current) if current else None
    if expected_workspace and expected_workspace != current_workspace:
        errors.append("workspace_base_hash_mismatch")
    errors = sorted(set(errors))
    return {
        "passed": not errors,
        "errors": errors,
        "expected_file_base_hashes": dict(sorted((str(k), str(v)) for k, v in expected.items())),
        "current_file_hashes": current,
        "current_workspace_hash": current_workspace,
        "canonical_mutation": False,
        "filesystem_mutation": False,
        "execution_allowed": False,
    }


__all__ = ["check_creative_approval_freshness"]


def compare_workspace_snapshot(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two existing WorkspaceSnapshot records without creating a new format."""

    def hashes(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        files = snapshot.get("files", [])
        return {str(item.get("path")): item.get("sha256") for item in files if isinstance(item, Mapping)}

    before = hashes(expected)
    after = hashes(actual)
    changes = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append({"path": path, "kind": "ADDED", "previous_sha256": None, "current_sha256": after[path]})
        elif path not in after:
            changes.append({"path": path, "kind": "REMOVED", "previous_sha256": before[path], "current_sha256": None})
        elif before[path] != after[path]:
            changes.append({"path": path, "kind": "MODIFIED", "previous_sha256": before[path], "current_sha256": after[path]})
    return {"passed": not changes, "changes": changes, "canonical_mutation": False, "filesystem_mutation": False}


__all__.append("compare_workspace_snapshot")
