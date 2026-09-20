"""Deterministic read-only enforcement boundary for structural findings.

Phase 38 produces evidence-backed findings but intentionally does not decide
whether a report blocks a workflow.  This module makes that decision explicit
without changing the report, assigning an architecture score, or performing
any repair or external operation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_policy import StructuralPolicyReport

STRUCTURAL_POLICY_GATE_SCHEMA = "structural.policy.gate.v1"
STRUCTURAL_POLICY_GATE_STATUSES = frozenset(
    {"PASS", "FAIL", "UNRESOLVED", "FAIL_AND_UNRESOLVED"}
)
STRUCTURAL_POLICY_GATE_EXIT_CODES = MappingProxyType(
    {"PASS": 0, "FAIL": 1, "UNRESOLVED": 2, "FAIL_AND_UNRESOLVED": 3}
)
_MAX_FINDING_IDS = 50_000


class StructuralPolicyGateError(ValueError):
    """Raised when a policy gate result is not passing or is malformed."""

    def __init__(self, result: StructuralPolicyGateResult | None = None, message: str = "") -> None:
        self.result = result
        super().__init__(message or (f"Structural policy gate blocked: {result.status}" if result else "Structural policy gate failed"))


def _text(value: Any, label: str, *, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuralPolicyGateError(message=f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.")
    result = value.strip()
    if len(result) > limit:
        raise StructuralPolicyGateError(message=f"{label}이(가) 너무 깁니다.")
    return result


def _finding_ids(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructuralPolicyGateError(message=f"{label}는 finding id 배열이어야 합니다.")
    if len(value) > _MAX_FINDING_IDS:
        raise StructuralPolicyGateError(message=f"{label} 항목이 너무 많습니다.")
    result = tuple(_text(item, label, limit=500) for item in value)
    if len(result) != len(set(result)):
        raise StructuralPolicyGateError(message=f"{label}에 중복 id가 있습니다.")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralPolicyGateResult:
    """Immutable decision and CI exit-code projection for one policy report."""

    report_id: str
    source_id: str
    source_hash: str
    status: str
    exit_code: int
    blocking_finding_ids: tuple[str, ...] = ()
    observed_finding_ids: tuple[str, ...] = ()
    unresolved_finding_ids: tuple[str, ...] = ()
    schema_version: str = STRUCTURAL_POLICY_GATE_SCHEMA
    id: str = ""
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_POLICY_GATE_SCHEMA:
            raise StructuralPolicyGateError(message="지원하지 않는 structural policy gate schema입니다.")
        object.__setattr__(self, "report_id", _text(self.report_id, "gate.report_id"))
        object.__setattr__(self, "source_id", _text(self.source_id, "gate.source_id"))
        object.__setattr__(self, "source_hash", _text(self.source_hash, "gate.source_hash", limit=240))
        status = _text(self.status, "gate.status", limit=40).upper()
        if status not in STRUCTURAL_POLICY_GATE_STATUSES:
            raise StructuralPolicyGateError(message=f"지원하지 않는 policy gate status입니다: {status}")
        object.__setattr__(self, "status", status)
        expected_exit_code = STRUCTURAL_POLICY_GATE_EXIT_CODES[status]
        if isinstance(self.exit_code, bool) or self.exit_code != expected_exit_code:
            raise StructuralPolicyGateError(message="gate status와 exit_code가 일치하지 않습니다.")
        blocking = _finding_ids(self.blocking_finding_ids, "gate.blocking_finding_ids")
        observed = _finding_ids(self.observed_finding_ids, "gate.observed_finding_ids")
        unresolved = _finding_ids(self.unresolved_finding_ids, "gate.unresolved_finding_ids")
        if set(observed) & set(unresolved):
            raise StructuralPolicyGateError(message="observed/unresolved finding id가 겹칩니다.")
        expected_blocking = tuple(sorted(set(observed) | set(unresolved)))
        if blocking != expected_blocking:
            raise StructuralPolicyGateError(message="blocking finding id가 결과와 일치하지 않습니다.")
        if status == "PASS" and blocking:
            raise StructuralPolicyGateError(message="PASS gate에는 blocking finding이 있을 수 없습니다.")
        if status == "FAIL" and (not observed or unresolved):
            raise StructuralPolicyGateError(message="FAIL gate는 observed finding만 가져야 합니다.")
        if status == "UNRESOLVED" and (observed or not unresolved):
            raise StructuralPolicyGateError(message="UNRESOLVED gate의 finding 구성이 잘못되었습니다.")
        if status == "FAIL_AND_UNRESOLVED" and (not observed or not unresolved):
            raise StructuralPolicyGateError(message="FAIL_AND_UNRESOLVED gate의 finding 구성이 잘못되었습니다.")
        object.__setattr__(self, "blocking_finding_ids", blocking)
        object.__setattr__(self, "observed_finding_ids", observed)
        object.__setattr__(self, "unresolved_finding_ids", unresolved)
        if not isinstance(self.canonical_mutation, bool) or self.canonical_mutation:
            raise StructuralPolicyGateError(message="gate.canonical_mutation은 false여야 합니다.")
        if not isinstance(self.filesystem_mutation, bool) or self.filesystem_mutation:
            raise StructuralPolicyGateError(message="gate.filesystem_mutation은 false여야 합니다.")
        gate_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-policy-gate:{gate_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralPolicyGateError(message="policy gate id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "status": self.status,
            "exit_code": self.exit_code,
            "blocking_finding_ids": list(self.blocking_finding_ids),
            "observed_finding_ids": list(self.observed_finding_ids),
            "unresolved_finding_ids": list(self.unresolved_finding_ids),
        }

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "report_id": self.report_id,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "status": self.status,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "blocking_finding_ids": list(self.blocking_finding_ids),
            "observed_finding_ids": list(self.observed_finding_ids),
            "unresolved_finding_ids": list(self.unresolved_finding_ids),
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }


def evaluate_structural_policy_gate(
    report: StructuralPolicyReport,
) -> StructuralPolicyGateResult:
    """Turn one existing policy report into an explicit deterministic decision."""

    if not isinstance(report, StructuralPolicyReport):
        raise StructuralPolicyGateError(message="report는 StructuralPolicyReport이어야 합니다.")
    observed_ids = tuple(finding.id for finding in report.violations)
    unresolved_ids = tuple(finding.id for finding in report.unresolved)
    if observed_ids and unresolved_ids:
        status = "FAIL_AND_UNRESOLVED"
    elif observed_ids:
        status = "FAIL"
    elif unresolved_ids:
        status = "UNRESOLVED"
    else:
        status = "PASS"
    return StructuralPolicyGateResult(
        report_id=report.id,
        source_id=report.source_id,
        source_hash=report.source_hash,
        status=status,
        exit_code=STRUCTURAL_POLICY_GATE_EXIT_CODES[status],
        blocking_finding_ids=tuple(sorted(observed_ids + unresolved_ids)),
        observed_finding_ids=observed_ids,
        unresolved_finding_ids=unresolved_ids,
    )


def assert_structural_policy_gate(report: StructuralPolicyReport) -> StructuralPolicyGateResult:
    """Return a passing result or raise with the immutable blocking result."""

    result = evaluate_structural_policy_gate(report)
    if not result.passed:
        raise StructuralPolicyGateError(result)
    return result


__all__ = [
    "STRUCTURAL_POLICY_GATE_EXIT_CODES",
    "STRUCTURAL_POLICY_GATE_SCHEMA",
    "STRUCTURAL_POLICY_GATE_STATUSES",
    "StructuralPolicyGateError",
    "StructuralPolicyGateResult",
    "assert_structural_policy_gate",
    "evaluate_structural_policy_gate",
]
