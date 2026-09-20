"""Approved baseline transitions for idea artifact hashes.

The baseline is the memory a later edit is measured against, so it may only move
on an explicit approval that is bound to the exact receipt it approved. Every move
is appended to the existing artifact ledger as one record carrying both sides of
the transition, which is what makes "when did this change, and against what was it
approved?" answerable later, and what makes rewinding possible at all.

This module owns no store. It validates, then writes through ``run.append_artifact``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.run import append_artifact, read_artifacts

BASELINE_TRANSITION_KIND = "idea-baseline-transition"
BASELINE_TRANSITION_SCHEMA = "idea.baseline-transition.v1"
_IMPACT_SCHEMA = "idea.reactive-impact.v1"


class BaselineAdoptionError(ValueError):
    """Raised when a baseline may not advance."""


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BaselineAdoptionError(f"{label} must be an object")
    return value


def _hashes(value: Any, label: str, *, allow_empty: bool = False) -> dict[str, str]:
    mapping = _require_mapping(value, label)
    if (not mapping and not allow_empty) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in mapping.items()
    ):
        raise BaselineAdoptionError(f"{label} must map artifact ids to hash strings")
    return dict(sorted(mapping.items()))


def _assert_adoptable(impact: Mapping[str, Any]) -> None:
    if impact.get("schema_version") != _IMPACT_SCHEMA:
        raise BaselineAdoptionError(f"impact_receipt must be {_IMPACT_SCHEMA}")
    if impact.get("status") != "PROPOSED":
        raise BaselineAdoptionError(
            f"an impact receipt with status {impact.get('status')} cannot become a baseline"
        )
    if impact.get("comparison_basis") == "BASELINE_MISSING":
        raise BaselineAdoptionError(
            "an undetermined comparison cannot become a baseline"
        )
    for field in ("blueprint_id", "seed_hash", "receipt_hash"):
        if not isinstance(impact.get(field), str) or not impact[field].strip():
            raise BaselineAdoptionError(f"impact_receipt.{field} is required")


def _assert_approved(approval: Mapping[str, Any], impact: Mapping[str, Any]) -> None:
    if approval.get("actor") != "Supervisor" or approval.get("status") != "APPROVED":
        raise BaselineAdoptionError("an explicit Supervisor approval is required")
    approved_at = approval.get("approved_at")
    if not isinstance(approved_at, str) or not approved_at.strip():
        raise BaselineAdoptionError("approval.approved_at is required")
    # The approval is bound to the exact receipt it approved; approving one result and
    # adopting another is the whole failure this check exists to stop.
    for field in ("blueprint_id", "receipt_hash"):
        if approval.get(field) != impact.get(field):
            raise BaselineAdoptionError(f"approval does not match impact_receipt.{field}")


def adopt_baseline(
    ledger_path: str | Path,
    *,
    run_id: str,
    scope: str,
    approval: Mapping[str, Any],
    impact_receipt: Mapping[str, Any],
    previous_baseline: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Record one approved baseline advance; write nothing when it is refused.

    ``scope`` identifies what the baseline is *for* and must stay stable while the
    content moves. A blueprint id must never be used: it is derived from the content,
    so an edit would change the key and orphan the very baseline it should be measured
    against. The blueprint id is recorded inside the transition instead.
    """

    impact = _require_mapping(impact_receipt, "impact_receipt")
    approved = _require_mapping(approval, "approval")
    if not isinstance(scope, str) or not scope.strip():
        raise BaselineAdoptionError("a stable baseline scope is required")
    _assert_adoptable(impact)
    _assert_approved(approved, impact)
    adopted = _hashes(impact.get("artifact_hashes"), "impact_receipt.artifact_hashes")
    previous = _hashes(previous_baseline, "previous_baseline") if previous_baseline is not None else None
    transition = {
        "schema_version": BASELINE_TRANSITION_SCHEMA,
        "scope": scope.strip(),
        "blueprint_id": impact["blueprint_id"],
        "seed_hash": impact["seed_hash"],
        "comparison_basis": impact.get("comparison_basis"),
        "impact_receipt_hash": impact["receipt_hash"],
        "from_baseline_hash": canonical_hash(previous) if previous is not None else None,
        "to_baseline_hash": canonical_hash(adopted),
        "changed_artifact_ids": list(impact.get("changed_artifact_ids") or ()),
        "affected_artifact_ids": list(impact.get("affected_artifact_ids") or ()),
        "artifact_hashes": adopted,
        # Both layers are recorded, so a later comparison can say why something moved
        # rather than only that it did.
        "artifact_basis_hashes": _hashes(
            impact.get("artifact_basis_hashes") or {}, "impact_receipt.artifact_basis_hashes",
            allow_empty=True,
        ),
        "artifact_authored_hashes": _hashes(
            impact.get("artifact_authored_hashes") or {}, "impact_receipt.artifact_authored_hashes",
            allow_empty=True,
        ),
        "approval": {
            "actor": approved["actor"],
            "status": approved["status"],
            "approved_at": approved["approved_at"],
        },
        "canonical_mutation": False,
        "execution_allowed": False,
    }
    # Content-addressed: replaying the identical adoption returns the existing record
    # instead of writing the same transition twice.
    return append_artifact(
        ledger_path, run_id=run_id, kind=BASELINE_TRANSITION_KIND,
        payload=transition, deduplicate=True,
    )


def read_baseline_history(
    ledger_path: str | Path,
    *,
    run_id: str,
    scope: str | None = None,
    blueprint_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return this run's baseline transitions in the order they were recorded.

    Filter by ``scope`` to follow one subject across its edits. ``blueprint_id`` is
    available for inspecting a single content version and is deliberately not the
    default: filtering by it would hide every transition made before the last edit.
    """

    history: list[dict[str, Any]] = []
    for record in read_artifacts(ledger_path):
        if record.get("run_id") != run_id or record.get("kind") != BASELINE_TRANSITION_KIND:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if scope is not None and payload.get("scope") != scope:
            continue
        if blueprint_id is not None and payload.get("blueprint_id") != blueprint_id:
            continue
        history.append({"artifact_id": record.get("artifact_id"), **dict(payload)})
    return history


def current_baseline(
    ledger_path: str | Path, *, run_id: str, scope: str
) -> dict[str, str] | None:
    """Project the baseline currently in force, or None when none was ever adopted.

    The ledger is the authority; this is a projection of its last accepted record,
    so the current baseline can never disagree with the history that produced it.
    """

    history = read_baseline_history(ledger_path, run_id=run_id, scope=scope)
    if not history:
        return None
    return dict(history[-1]["artifact_hashes"])


def current_baseline_layers(
    ledger_path: str | Path, *, run_id: str, scope: str
) -> dict[str, dict[str, str]] | None:
    """Project both identity layers of the baseline in force, when they were recorded.

    Transitions written before the layers existed carry empty maps; a comparison
    against those reports ``UNKNOWN`` rather than guessing a reason.
    """

    history = read_baseline_history(ledger_path, run_id=run_id, scope=scope)
    if not history:
        return None
    latest = history[-1]
    return {
        "content": dict(latest.get("artifact_hashes") or {}),
        "basis": dict(latest.get("artifact_basis_hashes") or {}),
        "authored": dict(latest.get("artifact_authored_hashes") or {}),
    }


__all__ = [
    "BASELINE_TRANSITION_KIND",
    "BASELINE_TRANSITION_SCHEMA",
    "BaselineAdoptionError",
    "adopt_baseline",
    "current_baseline",
    "current_baseline_layers",
    "read_baseline_history",
]
