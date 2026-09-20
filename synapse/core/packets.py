"""Small, immutable contracts for bounded worker handoffs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

_MAX_HINT = 1_000_000


def _required(data: Mapping[str, Any], fields: frozenset[str]) -> None:
    missing = sorted(fields - data.keys())
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")


def _text(data: Mapping[str, Any], name: str) -> str:
    value = data[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _bounded_hint(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_HINT:
        raise ValueError(f"context_budget_hint must be an integer between 0 and {_MAX_HINT}")
    return value


def _extras(data: Mapping[str, Any], known: frozenset[str]) -> dict[str, Any]:
    # JSON round-trip gives deterministic deep copies and rejects non-serializable values.
    try:
        return json.loads(json.dumps({k: data[k] for k in sorted(data.keys() - known)}, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError("unknown fields must contain JSON values") from exc


@dataclass(frozen=True, slots=True)
class TaskPacket:
    task_id: str
    objective: str
    scope: Any
    relevant_paths: Any
    known_state: Any
    constraints: Any
    acceptance_criteria: Any
    tests: Any
    stop_conditions: Any
    preferred_capability: Any
    context_budget_hint: int
    unknown: dict[str, Any]

    REQUIRED: ClassVar[frozenset[str]] = frozenset(("task_id", "objective", "scope", "relevant_paths", "known_state", "constraints", "acceptance_criteria", "tests", "stop_conditions", "preferred_capability", "context_budget_hint"))

    def __post_init__(self) -> None:
        object.__setattr__(self, "unknown", _extras(self.unknown, frozenset()))
        _text(self.to_record(), "task_id")
        _text(self.to_record(), "objective")
        _bounded_hint(self.context_budget_hint)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> TaskPacket:
        if not isinstance(record, Mapping):
            raise ValueError("TASK_PACKET must be an object")  # noqa: TRY004 - packet decoding preserves its tested ValueError contract
        _required(record, cls.REQUIRED)
        return cls(**{k: record[k] for k in cls.REQUIRED}, unknown=_extras(record, cls.REQUIRED))

    def to_record(self) -> dict[str, Any]:
        result = {k: getattr(self, k) for k in sorted(self.REQUIRED)}
        result.update(self.unknown)
        return json.loads(json.dumps(result, sort_keys=True))

    def to_json(self) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ResultPacket:
    task_id: str
    status: Any
    changed_files: Any
    commands_executed: Any
    test_result: Any
    relevant_errors: Any
    unresolved_issues: Any
    risk: Any
    escalation_required: Any
    short_summary: Any
    unknown: dict[str, Any]

    REQUIRED: ClassVar[frozenset[str]] = frozenset(("task_id", "status", "changed_files", "commands_executed", "test_result", "relevant_errors", "unresolved_issues", "risk", "escalation_required", "short_summary"))

    def __post_init__(self) -> None:
        object.__setattr__(self, "unknown", _extras(self.unknown, frozenset()))
        _text(self.to_record(), "task_id")

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ResultPacket:
        if not isinstance(record, Mapping):
            raise ValueError("RESULT_PACKET must be an object")  # noqa: TRY004 - packet decoding preserves its tested ValueError contract
        _required(record, cls.REQUIRED)
        return cls(**{k: record[k] for k in cls.REQUIRED}, unknown=_extras(record, cls.REQUIRED))

    def to_record(self) -> dict[str, Any]:
        result = {k: getattr(self, k) for k in sorted(self.REQUIRED)}
        result.update(self.unknown)
        return json.loads(json.dumps(result, sort_keys=True))

    def to_json(self) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def task_packet_from_json(value: str) -> TaskPacket:
    return TaskPacket.from_record(json.loads(value))


def result_packet_from_json(value: str) -> ResultPacket:
    return ResultPacket.from_record(json.loads(value))
