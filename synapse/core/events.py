"""Immutable local Event Bus and Observation Layer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.bundle import BundleReport
from synapse.core.cards import RouterEvent


class EventError(ValueError):
    """Raised when an event envelope or observation is invalid."""


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    id: str
    type: str
    source_id: str
    payload: Mapping[str, Any]
    sequence: int = 0

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.type.strip() or not self.source_id.strip():
            raise EventError("EventEnvelope id/type/source_id가 필요합니다.")
        if self.sequence < 0:
            raise EventError("EventEnvelope sequence는 0 이상이어야 합니다.")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "source_id": self.source_id,
            "payload": dict(self.payload),
            "sequence": self.sequence,
        }


def make_event(event_type: str, source_id: str, payload: Mapping[str, Any]) -> EventEnvelope:
    canonical = json.dumps(
        {"type": event_type, "source_id": source_id, "payload": dict(payload)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return EventEnvelope(
        id=f"event:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        type=event_type,
        source_id=source_id,
        payload=payload,
    )


class EventBus:
    """In-memory ordered bus with deterministic replay; no worker dispatch."""

    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._subscribers: dict[str, list[Callable[[EventEnvelope], None]]] = {}

    def subscribe(self, event_type: str, callback: Callable[[EventEnvelope], None]) -> None:
        if not event_type.strip():
            raise EventError("subscribe event_type가 비어 있습니다.")
        self._subscribers.setdefault(event_type, []).append(callback)

    def emit(self, event: EventEnvelope) -> EventEnvelope:
        if any(existing.id == event.id for existing in self._events):
            raise EventError(f"중복 event id입니다: {event.id}")
        committed = EventEnvelope(
            id=event.id,
            type=event.type,
            source_id=event.source_id,
            payload=event.payload,
            sequence=len(self._events) + 1,
        )
        self._events.append(committed)
        for callback in self._subscribers.get(committed.type, ()):
            callback(committed)
        return committed

    def events(self) -> tuple[EventEnvelope, ...]:
        return tuple(self._events)

    def replay(self, *, after_sequence: int = 0) -> tuple[EventEnvelope, ...]:
        if after_sequence < 0:
            raise EventError("after_sequence는 0 이상이어야 합니다.")
        return tuple(event for event in self._events if event.sequence > after_sequence)


def observe_bundle(report: BundleReport) -> RouterEvent:
    """Convert a reviewed BundleReport into a Card Router event only."""
    marker_counts = report.content_summary.get("marker_file_counts", {})
    signals = tuple(sorted(marker_counts)) if isinstance(marker_counts, Mapping) else ()
    evidence = (report.source_id, *report.key_files)
    return RouterEvent(
        id=f"event:bundle:{report.archive_sha256}",
        kind="bundle_intake",
        signals=signals,
        evidence=evidence,
        attributes={
            "source_id": report.source_id,
            "archive_sha256": report.archive_sha256,
            "entry_count": report.entry_count,
            "safe": report.safe,
        },
    )


def observe_file(path: str, *, source_id: str, change: str = "file_modified") -> RouterEvent:
    normalized = path.replace("\\", "/").strip()
    if not normalized or not source_id.strip():
        raise EventError("file observation path/source_id가 필요합니다.")
    return RouterEvent(
        id=f"event:file:{hashlib.sha256(f'{source_id}:{normalized}:{change}'.encode()).hexdigest()}",
        kind=change,
        signals=("file",),
        evidence=(source_id, normalized),
        attributes={"path": normalized, "source_id": source_id},
    )


__all__ = ["EventBus", "EventEnvelope", "EventError", "make_event", "observe_bundle", "observe_file"]
