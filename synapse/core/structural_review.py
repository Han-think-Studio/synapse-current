"""Explicit observation-to-policy review pipeline for Phase 43.

The pipeline is intentionally a small composition boundary.  A caller must
explicitly invoke it with one Phase 41 process request; the imported
observation then flows through the existing Phase 38 policy evaluator and
Phase 42 policy gate.  No watcher, event subscription, retry, or mutation is
owned here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.infigraph_process import (
    ExternalSensorImport,
    ExternalSensorProcessRequest,
    InfigraphProcessAdapter,
)
from synapse.core.structural_policy import (
    BUILT_IN_STRUCTURAL_POLICY_RULES,
    StructuralPolicyReport,
    StructuralPolicyRule,
    evaluate_structural_policy,
)
from synapse.core.structural_policy_gate import (
    StructuralPolicyGateResult,
    evaluate_structural_policy_gate,
)

STRUCTURAL_REVIEW_SCHEMA = "structural.review.v1"
STRUCTURAL_REVIEW_STAGES = (
    "PROCESS_ACQUIRED",
    "OBSERVATION_IMPORTED",
    "POLICY_EVALUATED",
    "POLICY_GATED",
)
_MAX_TEXT = 500


class StructuralReviewError(ValueError):
    """Raised when the explicit structural review binding is unsafe."""


def _text(value: Any, label: str, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuralReviewError(f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.")
    result = value.strip()
    if len(result) > limit:
        raise StructuralReviewError(f"{label}이(가) 너무 깁니다.")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralReviewResult:
    """Immutable binding of one explicit process-to-policy review."""

    request_id: str
    process_import: ExternalSensorImport
    policy_report: StructuralPolicyReport
    policy_gate: StructuralPolicyGateResult
    expected_workspace_hash: str | None = None
    stages: tuple[str, ...] = STRUCTURAL_REVIEW_STAGES
    schema_version: str = STRUCTURAL_REVIEW_SCHEMA
    id: str = ""
    automatic: bool = False
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_REVIEW_SCHEMA:
            raise StructuralReviewError("지원하지 않는 structural review schema입니다.")
        object.__setattr__(self, "request_id", _text(self.request_id, "review.request_id"))
        if not isinstance(self.process_import, ExternalSensorImport):
            raise StructuralReviewError("review.process_import 타입이 잘못되었습니다.")
        if not isinstance(self.policy_report, StructuralPolicyReport):
            raise StructuralReviewError("review.policy_report 타입이 잘못되었습니다.")
        if not isinstance(self.policy_gate, StructuralPolicyGateResult):
            raise StructuralReviewError("review.policy_gate 타입이 잘못되었습니다.")
        stages = tuple(self.stages)
        if stages != STRUCTURAL_REVIEW_STAGES:
            raise StructuralReviewError("structural review 단계 순서가 계약과 다릅니다.")
        object.__setattr__(self, "stages", stages)
        if self.expected_workspace_hash is not None:
            object.__setattr__(
                self,
                "expected_workspace_hash",
                _text(self.expected_workspace_hash, "review.expected_workspace_hash", limit=240),
            )

        observation = self.process_import.observation
        receipt = self.process_import.process_receipt
        if receipt.request_id != self.request_id:
            raise StructuralReviewError("review request identity가 process receipt와 다릅니다.")
        if (
            self.policy_report.source_id != observation.source_id
            or self.policy_report.source_hash != observation.observation_hash
        ):
            raise StructuralReviewError("policy report identity가 imported observation과 다릅니다.")
        if (
            self.policy_gate.report_id != self.policy_report.id
            or self.policy_gate.source_id != self.policy_report.source_id
            or self.policy_gate.source_hash != self.policy_report.source_hash
        ):
            raise StructuralReviewError("policy gate identity가 policy report와 다릅니다.")
        if (
            self.expected_workspace_hash is not None
            and observation.workspace_hash != self.expected_workspace_hash
        ):
            raise StructuralReviewError("expected workspace hash가 imported observation과 다릅니다.")
        for value, label in (
            (self.automatic, "review.automatic"),
            (self.canonical_mutation, "review.canonical_mutation"),
            (self.filesystem_mutation, "review.filesystem_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise StructuralReviewError(f"{label}은(는) false여야 합니다.")

        review_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-review:{review_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralReviewError("structural review id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "process_import_id": self.process_import.process_receipt.id,
            "observation_id": self.process_import.observation.id,
            "observation_hash": self.process_import.observation.observation_hash,
            "policy_report_id": self.policy_report.id,
            "policy_report_hash": self.policy_report.report_hash,
            "policy_gate_id": self.policy_gate.id,
            "policy_gate_status": self.policy_gate.status,
            "policy_gate_exit_code": self.policy_gate.exit_code,
            "expected_workspace_hash": self.expected_workspace_hash,
            "stages": list(self.stages),
        }

    @property
    def observation(self):
        """Return the imported observation without copying or mutating it."""

        return self.process_import.observation

    @property
    def passed(self) -> bool:
        return self.policy_gate.passed

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "request_id": self.request_id,
            "stages": list(self.stages),
            "expected_workspace_hash": self.expected_workspace_hash,
            "process_import": self.process_import.to_record(),
            "policy_report": self.policy_report.to_record(),
            "policy_gate": self.policy_gate.to_record(),
            "passed": self.passed,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }


def run_explicit_structural_review(
    request: ExternalSensorProcessRequest,
    *,
    adapter: InfigraphProcessAdapter | None = None,
    rules: Sequence[StructuralPolicyRule] = BUILT_IN_STRUCTURAL_POLICY_RULES,
    expected_workspace_hash: str | None = None,
) -> StructuralReviewResult:
    """Run one explicit process and bind its observation to policy and gate results."""

    if not isinstance(request, ExternalSensorProcessRequest):
        raise StructuralReviewError("request는 ExternalSensorProcessRequest이어야 합니다.")
    if adapter is not None and not isinstance(adapter, InfigraphProcessAdapter):
        raise StructuralReviewError("adapter는 InfigraphProcessAdapter이어야 합니다.")
    if expected_workspace_hash is not None:
        expected_workspace_hash = _text(
            expected_workspace_hash,
            "expected_workspace_hash",
            limit=240,
        )

    process_import = (adapter or InfigraphProcessAdapter()).execute(request)
    if (
        expected_workspace_hash is not None
        and process_import.observation.workspace_hash != expected_workspace_hash
    ):
        raise StructuralReviewError("expected workspace hash가 imported observation과 다릅니다.")
    policy_report = evaluate_structural_policy(process_import.observation, rules=rules)
    policy_gate = evaluate_structural_policy_gate(policy_report)
    return StructuralReviewResult(
        request_id=request.request_id,
        process_import=process_import,
        policy_report=policy_report,
        policy_gate=policy_gate,
        expected_workspace_hash=expected_workspace_hash,
    )


__all__ = [
    "STRUCTURAL_REVIEW_SCHEMA",
    "STRUCTURAL_REVIEW_STAGES",
    "StructuralReviewError",
    "StructuralReviewResult",
    "run_explicit_structural_review",
]
