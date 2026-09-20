"""Bind a verified structural report to the existing bounded context projection.

Phase 51 verifies the report file.  This module is the next explicit,
downstream-only boundary: it accepts that verified in-memory inspection and a
caller-supplied ``StructuralContextRequest``, then reuses the Phase 37 context
builder.  It does not read the report again, enforce policy, or call an
executor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_context import (
    StructuralContext,
    StructuralContextError,
    StructuralContextRequest,
    build_bounded_structural_context,
)
from synapse.core.structural_export_report_inspection import (
    StructuralExportReportInspection,
)

STRUCTURAL_REPORT_CONTEXT_SCHEMA = "structural.report.context.v1"
STRUCTURAL_REPORT_CONTEXT_STAGES = (
    "REPORT_INSPECTED",
    "CONTEXT_REQUEST_BOUND",
    "CONTEXT_PROJECTED",
)


class StructuralReportContextError(ValueError):
    """Raised when a verified report cannot be bound to a context request."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralReportContextResult:
    """Immutable lineage wrapper for one verified report and bounded context."""

    inspection: StructuralExportReportInspection
    request: StructuralContextRequest
    context: StructuralContext
    stages: tuple[str, ...] = STRUCTURAL_REPORT_CONTEXT_STAGES
    schema_version: str = STRUCTURAL_REPORT_CONTEXT_SCHEMA
    id: str = ""
    automatic: bool = False
    filesystem_mutation: bool = False
    source_mutation: bool = False
    workspace_mutation: bool = False
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_REPORT_CONTEXT_SCHEMA:
            raise StructuralReportContextError(
                "지원하지 않는 structural report context schema입니다."
            )
        if not isinstance(self.inspection, StructuralExportReportInspection):
            raise StructuralReportContextError("result.inspection 타입이 잘못되었습니다.")
        if not isinstance(self.request, StructuralContextRequest):
            raise StructuralReportContextError("result.request 타입이 잘못되었습니다.")
        if not isinstance(self.context, StructuralContext):
            raise StructuralReportContextError("result.context 타입이 잘못되었습니다.")
        stages = tuple(self.stages)
        if stages != STRUCTURAL_REPORT_CONTEXT_STAGES:
            raise StructuralReportContextError("report context 단계 순서가 계약과 다릅니다.")
        object.__setattr__(self, "stages", stages)

        observation = self.inspection.report.review.observation
        if self.request.source_hash != observation.observation_hash:
            raise StructuralReportContextError(
                "context request source_hash가 verified report observation과 다릅니다."
            )
        if self.context.request != self.request:
            raise StructuralReportContextError(
                "context request가 result request와 일치하지 않습니다."
            )
        if (
            self.context.source_id != observation.id
            or self.context.source_hash != observation.observation_hash
        ):
            raise StructuralReportContextError(
                "context source identity가 verified report observation과 다릅니다."
            )
        for value, label in (
            (self.automatic, "result.automatic"),
            (self.filesystem_mutation, "result.filesystem_mutation"),
            (self.source_mutation, "result.source_mutation"),
            (self.workspace_mutation, "result.workspace_mutation"),
            (self.canonical_mutation, "result.canonical_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise StructuralReportContextError(f"{label}은(는) false여야 합니다.")

        context_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-report-context:{context_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralReportContextError("report context id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        observation = self.inspection.report.review.observation
        return {
            "schema_version": self.schema_version,
            "inspection_id": self.inspection.id,
            "report_id": self.inspection.report.id,
            "review_id": self.inspection.report.review.id,
            "observation_id": observation.id,
            "observation_hash": observation.observation_hash,
            "request": self.request.to_record(),
            "context_id": self.context.id,
            "context_hash": self.context.context_hash,
            "stages": list(self.stages),
        }

    @property
    def report_gate_status(self) -> str:
        return self.inspection.report.review.policy_gate.status

    @property
    def report_gate_exit_code(self) -> int:
        return self.inspection.report.gate_exit_code

    @property
    def report_gate_passed(self) -> bool:
        return self.inspection.report.passed

    def to_record(self) -> dict[str, Any]:
        review = self.inspection.report.review
        observation = review.observation
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": "verified_structural_report_context",
            "stages": list(self.stages),
            "inspection": {
                "id": self.inspection.id,
                "report_id": self.inspection.report.id,
                "review_id": review.id,
                "report_file_sha256": self.inspection.report_file_sha256,
            },
            "report": {
                "input_sha256": review.input_sha256,
                "observation_id": observation.id,
                "observation_hash": observation.observation_hash,
                "workspace_hash": observation.workspace_hash,
                "gate": {
                    "status": self.report_gate_status,
                    "passed": self.report_gate_passed,
                    "exit_code": self.report_gate_exit_code,
                },
            },
            "request": self.request.to_record(),
            "context": self.context.to_record(),
            "context_projected": True,
            "automatic": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def build_bounded_context_from_verified_report(
    inspection: StructuralExportReportInspection,
    request: StructuralContextRequest,
) -> StructuralReportContextResult:
    """Project one explicit bounded context from an already verified report."""

    if not isinstance(inspection, StructuralExportReportInspection):
        raise StructuralReportContextError(
            "inspection은 StructuralExportReportInspection이어야 합니다."
        )
    if not isinstance(request, StructuralContextRequest):
        raise StructuralReportContextError(
            "request는 StructuralContextRequest이어야 합니다."
        )
    observation = inspection.report.review.observation
    if request.source_hash != observation.observation_hash:
        raise StructuralReportContextError(
            "context request source_hash가 verified report observation과 다릅니다."
        )
    try:
        context = build_bounded_structural_context(observation, request)
    except StructuralContextError as exc:
        raise StructuralReportContextError(str(exc)) from exc
    return StructuralReportContextResult(
        inspection=inspection,
        request=request,
        context=context,
    )


__all__ = [
    "STRUCTURAL_REPORT_CONTEXT_SCHEMA",
    "STRUCTURAL_REPORT_CONTEXT_STAGES",
    "StructuralReportContextError",
    "StructuralReportContextResult",
    "build_bounded_context_from_verified_report",
]
