"""Durable, self-validating persistence for Canonical State.

This is the Phase 3 boundary. It stores the current registry, revision, and
reactive history without replaying commits on load. The JSON checksum detects
truncation or edits that do not update the integrity field; semantic validation
then rejects malformed contracts and invariant violations.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from synapse.core.contracts import (
    Conflict,
    Decision,
    Dependency,
    Entity,
    Fact,
    Invariant,
    LifecycleStatus,
    Projection,
    Provenance,
    StateItem,
    to_record,
)
from synapse.core.errors import InvariantViolation
from synapse.core.invariants import validate_core
from synapse.core.registry import CanonicalRegistry, ReactiveMark

FORMAT = "synapse-canonical-state"
FORMAT_VERSION = 1

_ITEM_TYPES: dict[str, type[StateItem]] = {
    "StateItem": StateItem,
    "Entity": Entity,
    "Fact": Fact,
    "Decision": Decision,
    "Dependency": Dependency,
    "Conflict": Conflict,
    "Invariant": Invariant,
    "Projection": Projection,
}
_TUPLE_FIELDS = {
    "provenance",
    "depends_on",
    "supersedes",
    "conflicts_with",
    "alternatives",
    "canonical_ids",
}


class PersistenceError(ValueError):
    """Raised when a durable Canonical State cannot be trusted or decoded."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _checksum(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _item_record(item: StateItem) -> dict[str, Any]:
    return {"type": type(item).__name__, "record": to_record(item)}


def _mark_record(mark: ReactiveMark) -> dict[str, Any]:
    return to_record(mark)


def _decode_item(value: Any) -> StateItem:
    if not isinstance(value, Mapping):
        raise PersistenceError("items/history 항목은 객체여야 합니다.")
    item_type = value.get("type")
    raw = value.get("record")
    cls = _ITEM_TYPES.get(str(item_type))
    if cls is None or not isinstance(raw, Mapping):
        raise PersistenceError(f"알 수 없거나 잘못된 StateItem type입니다: {item_type!r}")
    data = dict(raw)
    try:
        data["status"] = LifecycleStatus(data["status"])
        data["provenance"] = tuple(Provenance(**record) for record in data.get("provenance", ()))
        for field_name in _TUPLE_FIELDS - {"provenance"}:
            if field_name in data:
                data[field_name] = tuple(data[field_name])
        return cls(**data)
    except InvariantViolation as exc:
        raise PersistenceError(f"StateItem invariant 검증에 실패했습니다: {item_type!r}: {exc}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(f"StateItem을 복원할 수 없습니다: {item_type!r}: {exc}") from exc


def _payload(registry: CanonicalRegistry) -> dict[str, Any]:
    history: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(registry.all(), key=lambda record: record.id):
        records = registry.history(item.id)
        if records:
            history[item.id] = [_item_record(record) for record in records]
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "revision": registry.revision,
        "items": [_item_record(item) for item in sorted(registry.all(), key=lambda record: record.id)],
        "history": history,
        "reactive_marks": [_mark_record(mark) for mark in registry.reactive_marks()],
    }


def save_registry(registry: CanonicalRegistry, path: str | Path) -> Path:
    """Atomically save a registry and return its resolved destination."""
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload(registry)
    document = {"payload": payload, "sha256": _checksum(payload)}
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_canonical_json(document) + b"\n")
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise PersistenceError(f"Canonical State를 저장할 수 없습니다: {destination}") from exc
    return destination


def _read_document(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PersistenceError(f"Canonical State 파일을 읽을 수 없습니다: {path}") from exc
    if not isinstance(document, Mapping):
        raise PersistenceError("Canonical State 최상위 문서는 객체여야 합니다.")
    payload = document.get("payload")
    digest = document.get("sha256")
    if not isinstance(payload, Mapping) or not isinstance(digest, str):
        raise PersistenceError("Canonical State integrity envelope가 없습니다.")
    if digest != _checksum(payload):
        raise PersistenceError("Canonical State checksum이 일치하지 않습니다.")
    return document


def _decode_mark(value: Any) -> ReactiveMark:
    if not isinstance(value, Mapping):
        raise PersistenceError("reactive_marks 항목은 객체여야 합니다.")
    data = dict(value)
    try:
        data["before"] = LifecycleStatus(data["before"])
        data["after"] = LifecycleStatus(data["after"])
        if data.get("provenance") is not None:
            data["provenance"] = Provenance(**data["provenance"])
        return ReactiveMark(**data)
    except (InvariantViolation, KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(f"ReactiveMark를 복원할 수 없습니다: {exc}") from exc


def load_registry(
    path: str | Path,
    *,
    repair_rules: Mapping[str, Any] | None = None,
) -> CanonicalRegistry:
    """Load and validate a registry without replaying or mutating history."""
    source = Path(path).expanduser().resolve()
    document = _read_document(source)
    payload = document["payload"]
    if payload.get("format") != FORMAT or payload.get("version") != FORMAT_VERSION:
        raise PersistenceError("지원하지 않는 Canonical State format/version입니다.")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise PersistenceError("Canonical State revision이 잘못되었습니다.")
    raw_items = payload.get("items")
    raw_history = payload.get("history", {})
    raw_marks = payload.get("reactive_marks", [])
    if not isinstance(raw_items, list) or not isinstance(raw_history, Mapping) or not isinstance(raw_marks, list):
        raise PersistenceError("Canonical State items/history 구조가 잘못되었습니다.")
    try:
        items = [_decode_item(value) for value in raw_items]
        if len({item.id for item in items}) != len(items):
            raise PersistenceError("Canonical State에 중복 item id가 있습니다.")
        history: dict[str, tuple[StateItem, ...]] = {}
        for item_id, records in raw_history.items():
            if not isinstance(records, list):
                raise PersistenceError("history record 목록이 아닙니다.")
            history[str(item_id)] = tuple(_decode_item(record) for record in records)
        marks = tuple(_decode_mark(value) for value in raw_marks)
        validate_core(items)
        for item_id, records in history.items():
            if item_id not in {item.id for item in items}:
                raise PersistenceError(f"history가 존재하지 않는 item을 가리킵니다: {item_id}")
            if any(record.id != item_id for record in records):
                raise PersistenceError(f"history key와 record id가 일치하지 않습니다: {item_id}")
            for record in records:
                validate_core([record])
        registry = CanonicalRegistry(repair_rules=repair_rules)
        registry._restore(
            items={item.id: item for item in items},
            history=history,
            revision=revision,
            reactive_marks=marks,
        )
        return registry
    except PersistenceError:
        raise
    except InvariantViolation as exc:
        raise PersistenceError(f"Canonical State invariant 검증에 실패했습니다: {exc}") from exc


__all__ = ["FORMAT", "FORMAT_VERSION", "PersistenceError", "load_registry", "save_registry"]
