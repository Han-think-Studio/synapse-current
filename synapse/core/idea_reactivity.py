"""Read-only dependency and impact receipts for all idea profiles."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from synapse.core.idea_artifacts import build_artifact_manifest
from synapse.core.idea_blueprint import IdeaBlueprint
from synapse.core.idea_session import canonical_hash


def build_reactive_impact_receipt(
    blueprint: IdeaBlueprint,
    *,
    changed_artifact_ids: Sequence[str] = (),
    changed_zones: Sequence[str] = (),
    baseline_artifact_hashes: Mapping[str, str] | None = None,
    baseline_basis_hashes: Mapping[str, str] | None = None,
    baseline_authored_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return direct/transitive impact and reuse candidates without mutation.

    The change set comes from exactly one source. ``baseline_artifact_hashes`` is an
    approved snapshot of this blueprint's artifact hashes; when it is supplied the
    change set is derived by comparing it with the current hashes. Otherwise the
    caller declares the change set itself. With neither, the receipt reports
    ``BASELINE_MISSING`` instead of claiming that nothing was affected -- an
    unanswerable question must not read as a clean answer.
    """

    if not isinstance(blueprint, IdeaBlueprint):
        raise TypeError("blueprint must be an IdeaBlueprint")
    artifacts = build_artifact_manifest(blueprint)
    by_id = {item.artifact_id: item for item in artifacts}
    by_zone = {item.zone: item for item in artifacts}
    current_hashes = {item.artifact_id: item.content_hash for item in artifacts}
    current_basis = {item.artifact_id: item.basis_hash for item in artifacts}
    current_authored = {item.artifact_id: item.authored_hash for item in artifacts}
    declared = bool(changed_artifact_ids) or bool(changed_zones)
    if baseline_artifact_hashes is not None and declared:
        raise ValueError(
            "supply either baseline_artifact_hashes or an explicit change set, not both"
        )
    if baseline_artifact_hashes is not None:
        if not isinstance(baseline_artifact_hashes, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in baseline_artifact_hashes.items()
        ):
            raise ValueError("baseline_artifact_hashes must map artifact ids to hash strings")
        comparison_basis = "BASELINE_COMPARED"
        changed_ids = {
            artifact_id
            for artifact_id, content_hash in current_hashes.items()
            if baseline_artifact_hashes.get(artifact_id) != content_hash
        }
        # An id the baseline knew but this blueprint no longer produces cannot be
        # propagated from; it is reported rather than silently dropped.
        changed_ids.update(set(baseline_artifact_hashes) - set(current_hashes))
        changed_zones = ()
    else:
        comparison_basis = "CALLER_DECLARED" if declared else "BASELINE_MISSING"
        changed_ids = {str(item).strip() for item in changed_artifact_ids if str(item).strip()}
    known_zones = set(by_zone)
    unknown_zones = sorted(set(changed_zones) - known_zones)
    changed_ids.update(by_zone[zone].artifact_id for zone in changed_zones if zone in by_zone)
    unknown_ids = sorted(changed_ids - set(by_id))
    reverse: dict[str, set[str]] = {item.artifact_id: set() for item in artifacts}
    for item in artifacts:
        for dependency_zone in item.depends_on:
            dependency = by_zone.get(dependency_zone)
            if dependency:
                reverse[dependency.artifact_id].add(item.artifact_id)
    affected = set(changed_ids & set(by_id))
    queue = deque(sorted(affected))
    while queue:
        current = queue.popleft()
        for dependent in sorted(reverse.get(current, ())):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
    # Why something moved decides what to do about it: a moved root means regenerate,
    # an authored edit means review what depends on it, and both at once is a conflict
    # a person has to settle. Saying only "it moved" cannot separate those.
    change_reasons: dict[str, str] = {}
    for artifact_id in sorted(changed_ids & set(by_id)):
        known_basis = baseline_basis_hashes.get(artifact_id) if baseline_basis_hashes else None
        known_authored = (
            baseline_authored_hashes.get(artifact_id) if baseline_authored_hashes else None
        )
        if known_basis is None or known_authored is None:
            change_reasons[artifact_id] = "UNKNOWN"
            continue
        basis_moved = known_basis != current_basis.get(artifact_id)
        authored_moved = known_authored != current_authored.get(artifact_id)
        if basis_moved and authored_moved:
            change_reasons[artifact_id] = "BOTH"
        elif basis_moved:
            change_reasons[artifact_id] = "ROOT_MOVED"
        elif authored_moved:
            change_reasons[artifact_id] = "AUTHORED"
        else:
            change_reasons[artifact_id] = "UNKNOWN"
    undetermined = comparison_basis == "BASELINE_MISSING"
    unresolved = bool(unknown_ids or unknown_zones) or undetermined
    reusable = sorted(item.artifact_id for item in artifacts if item.artifact_id not in affected and not unresolved)
    payload = {
        "schema_version": "idea.reactive-impact.v1",
        "blueprint_id": blueprint.id,
        "seed_hash": blueprint.seed_hash,
        "comparison_basis": comparison_basis,
        "artifact_hashes": current_hashes,
        "artifact_basis_hashes": current_basis,
        "artifact_authored_hashes": current_authored,
        "changed_artifact_ids": sorted(changed_ids),
        "change_reasons": change_reasons,
        "unknown_changed_zones": unknown_zones,
        "unknown_changed_ids": unknown_ids,
        "affected_artifact_ids": sorted(affected),
        "reusable_candidates": reusable,
        "status": "UNRESOLVED" if unresolved else "PROPOSED",
        "proposal_only": True,
        "filesystem_mutation": False,
        "execution_allowed": False,
    }
    payload["receipt_hash"] = canonical_hash(payload)
    return payload


__all__ = ["build_reactive_impact_receipt"]
