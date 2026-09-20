"""Explicit bridge from existing workspace observations to the EventBus.

Phase 39 does not watch, rescan, or invoke a sensor.  It turns only the
already-observed workspace changes into immutable invalidation records and
emits them through the existing in-memory EventBus when the caller explicitly
asks for emission.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from synapse.core.events import EventBus, EventEnvelope, make_event
from synapse.core.idea_session import canonical_hash
from synapse.core.workspace import WorkspaceObservation


class StructuralReactiveError(ValueError):
    """Raised when a workspace-to-structure invalidation is unsafe."""


STRUCTURAL_INVALIDATION_EVENT = "structural_observation_invalidated"
STRUCTURAL_INVALIDATION_KINDS = frozenset({"ADDED", "MODIFIED", "REMOVED"})
_MAX_INVALIDATIONS = 50_000
_MAX_TEXT = 4_000


def _text(value: Any, label: str, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuralReactiveError(f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.")
    result = value.strip()
    if len(result) > limit:
        raise StructuralReactiveError(f"{label}이(가) 너무 깁니다.")
    return result


def _relative_path(value: Any) -> str:
    raw = _text(value, "invalidation.path", limit=500).replace("\\", "/")
    if raw.startswith("/") or ":" in raw:
        raise StructuralReactiveError(f"안전하지 않은 invalidation path입니다: {value}")
    parts = tuple(part for part in raw.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise StructuralReactiveError(f"안전하지 않은 invalidation path입니다: {value}")
    return "/".join(parts)


def _optional_hash(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label, limit=240)


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralInvalidation:
    """One explicit request for a later structural sensor re-check."""

    plan_id: str
    workspace_path: str
    workspace_snapshot_hash: str
    path: str
    change_kind: str
    previous_sha256: str | None = None
    current_sha256: str | None = None
    id: str = ""
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _text(self.plan_id, "invalidation.plan_id", limit=500))
        object.__setattr__(
            self,
            "workspace_path",
            _text(self.workspace_path, "invalidation.workspace_path", limit=2_000),
        )
        object.__setattr__(
            self,
            "workspace_snapshot_hash",
            _text(self.workspace_snapshot_hash, "invalidation.workspace_snapshot_hash", limit=240),
        )
        object.__setattr__(self, "path", _relative_path(self.path))
        change_kind = _text(self.change_kind, "invalidation.change_kind", limit=40).upper()
        if change_kind not in STRUCTURAL_INVALIDATION_KINDS:
            raise StructuralReactiveError(f"지원하지 않는 invalidation change kind입니다: {change_kind}")
        object.__setattr__(self, "change_kind", change_kind)
        previous = _optional_hash(self.previous_sha256, "invalidation.previous_sha256")
        current = _optional_hash(self.current_sha256, "invalidation.current_sha256")
        if change_kind == "ADDED" and current is None:
            raise StructuralReactiveError("ADDED invalidation에는 current hash가 필요합니다.")
        if change_kind == "REMOVED" and previous is None:
            raise StructuralReactiveError("REMOVED invalidation에는 previous hash가 필요합니다.")
        if change_kind == "MODIFIED" and previous == current:
            raise StructuralReactiveError("MODIFIED invalidation은 hash가 달라야 합니다.")
        object.__setattr__(self, "previous_sha256", previous)
        object.__setattr__(self, "current_sha256", current)
        if not isinstance(self.canonical_mutation, bool) or self.canonical_mutation:
            raise StructuralReactiveError("invalidation.canonical_mutation은 false여야 합니다.")
        if not isinstance(self.filesystem_mutation, bool) or self.filesystem_mutation:
            raise StructuralReactiveError("invalidation.filesystem_mutation은 false여야 합니다.")
        invalidation_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-invalidation:{invalidation_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralReactiveError("StructuralInvalidation id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, str | None]:
        return {
            "plan_id": self.plan_id,
            "workspace_path": self.workspace_path,
            "workspace_snapshot_hash": self.workspace_snapshot_hash,
            "path": self.path,
            "change_kind": self.change_kind,
            "previous_sha256": self.previous_sha256,
            "current_sha256": self.current_sha256,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "event_type": STRUCTURAL_INVALIDATION_EVENT,
            "plan_id": self.plan_id,
            "workspace_path": self.workspace_path,
            "workspace_snapshot_hash": self.workspace_snapshot_hash,
            "path": self.path,
            "change_kind": self.change_kind,
            "previous_sha256": self.previous_sha256,
            "current_sha256": self.current_sha256,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralInvalidationBatch:
    """All changed paths from one already-completed workspace observation."""

    plan_id: str
    workspace_path: str
    workspace_snapshot_hash: str
    invalidations: tuple[StructuralInvalidation, ...] = ()
    id: str = ""
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _text(self.plan_id, "batch.plan_id", limit=500))
        object.__setattr__(self, "workspace_path", _text(self.workspace_path, "batch.workspace_path", limit=2_000))
        object.__setattr__(
            self,
            "workspace_snapshot_hash",
            _text(self.workspace_snapshot_hash, "batch.workspace_snapshot_hash", limit=240),
        )
        if isinstance(self.invalidations, (str, bytes)) or not isinstance(self.invalidations, Sequence):
            raise StructuralReactiveError("batch.invalidations는 배열이어야 합니다.")
        if len(self.invalidations) > _MAX_INVALIDATIONS:
            raise StructuralReactiveError("batch.invalidations 항목이 너무 많습니다.")
        invalidations = tuple(self.invalidations)
        if any(not isinstance(item, StructuralInvalidation) for item in invalidations):
            raise StructuralReactiveError("batch.invalidations에는 StructuralInvalidation만 허용됩니다.")
        for item in invalidations:
            if (
                item.plan_id != self.plan_id
                or item.workspace_path != self.workspace_path
                or item.workspace_snapshot_hash != self.workspace_snapshot_hash
            ):
                raise StructuralReactiveError("invalidation의 workspace binding이 batch와 다릅니다.")
        invalidations = tuple(sorted(invalidations, key=lambda item: (item.path, item.change_kind, item.id)))
        if len({item.id for item in invalidations}) != len(invalidations):
            raise StructuralReactiveError("batch.invalidations에 중복 id가 있습니다.")
        object.__setattr__(self, "invalidations", invalidations)
        if not isinstance(self.canonical_mutation, bool) or self.canonical_mutation:
            raise StructuralReactiveError("batch.canonical_mutation은 false여야 합니다.")
        if not isinstance(self.filesystem_mutation, bool) or self.filesystem_mutation:
            raise StructuralReactiveError("batch.filesystem_mutation은 false여야 합니다.")
        batch_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-invalidation-batch:{batch_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralReactiveError("StructuralInvalidationBatch id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "workspace_path": self.workspace_path,
            "workspace_snapshot_hash": self.workspace_snapshot_hash,
            "invalidations": [item.to_record() for item in self.invalidations],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "workspace_path": self.workspace_path,
            "workspace_snapshot_hash": self.workspace_snapshot_hash,
            "invalidations": [item.to_record() for item in self.invalidations],
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def _workspace_snapshot_hash(observation: WorkspaceObservation) -> str:
    snapshot = observation.snapshot
    return canonical_hash(
        {
            "plan_id": snapshot.plan_id,
            "workspace_path": snapshot.workspace_path,
            "files": [
                {
                    "path": item.path,
                    "status": item.status,
                    "sha256": item.sha256,
                }
                for item in snapshot.files
            ],
        }
    )


def build_structural_invalidation_batch(
    observation: WorkspaceObservation,
) -> StructuralInvalidationBatch:
    """Convert only supplied workspace changes into a deterministic batch."""

    if not isinstance(observation, WorkspaceObservation):
        raise StructuralReactiveError("observation은 WorkspaceObservation이어야 합니다.")
    snapshot = observation.snapshot
    snapshot_hash = _workspace_snapshot_hash(observation)
    invalidations = tuple(
        StructuralInvalidation(
            plan_id=snapshot.plan_id,
            workspace_path=snapshot.workspace_path,
            workspace_snapshot_hash=snapshot_hash,
            path=change.path,
            change_kind=change.kind,
            previous_sha256=change.previous_sha256,
            current_sha256=change.current_sha256,
        )
        for change in observation.changes
    )
    return StructuralInvalidationBatch(
        plan_id=snapshot.plan_id,
        workspace_path=snapshot.workspace_path,
        workspace_snapshot_hash=snapshot_hash,
        invalidations=invalidations,
    )


def emit_structural_invalidations(
    event_bus: EventBus,
    batch: StructuralInvalidationBatch,
) -> tuple[EventEnvelope, ...]:
    """Explicitly emit a batch through the existing EventBus only."""

    if not isinstance(event_bus, EventBus):
        raise StructuralReactiveError("event_bus은 EventBus여야 합니다.")
    if not isinstance(batch, StructuralInvalidationBatch):
        raise StructuralReactiveError("batch는 StructuralInvalidationBatch여야 합니다.")
    emitted: list[EventEnvelope] = []
    for invalidation in batch.invalidations:
        event = make_event(
            STRUCTURAL_INVALIDATION_EVENT,
            invalidation.id,
            {
                "batch_id": batch.id,
                "invalidation": invalidation.to_record(),
            },
        )
        emitted.append(event_bus.emit(event))
    return tuple(emitted)


__all__ = [
    "STRUCTURAL_INVALIDATION_EVENT",
    "STRUCTURAL_INVALIDATION_KINDS",
    "StructuralInvalidation",
    "StructuralInvalidationBatch",
    "StructuralReactiveError",
    "build_structural_invalidation_batch",
    "emit_structural_invalidations",
]
