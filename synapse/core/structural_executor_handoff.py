"""Prepare a bounded structural context for an explicit executor handoff.

Phase 52 produces a verified, bounded, non-authoritative context.  This module
binds that result to the existing Action Router so a caller can inspect the
selected executor route without creating a RuntimeRequest or dispatching it.
The handoff is deliberately a preparation envelope, not an execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from synapse.core.action import ActionRequest, ActionRoute, ActionRouter
from synapse.core.idea_session import canonical_hash
from synapse.core.structural_report_context import StructuralReportContextResult

STRUCTURAL_EXECUTOR_HANDOFF_SCHEMA = "structural.executor.handoff.v1"
STRUCTURAL_EXECUTOR_HANDOFF_STAGES = (
    "CONTEXT_VERIFIED",
    "ACTION_ROUTED",
    "HANDOFF_PREPARED",
)
STRUCTURAL_EXECUTOR_HANDOFF_STATUSES = frozenset({"READY", "REVIEW", "BLOCKED"})


class StructuralExecutorHandoffError(ValueError):
    """Raised when a structural context cannot cross the handoff boundary."""


def _expected_status_and_reason(
    context_result: StructuralReportContextResult,
    action_request: ActionRequest,
    route: ActionRoute,
) -> tuple[str, str]:
    if route.status == "BLOCKED":
        return "BLOCKED", route.reason
    if context_result.report_gate_status != "PASS":
        return "REVIEW", "verified structural gate가 PASS가 아니므로 executor handoff는 review-only입니다."
    if not action_request.approval_required:
        return "REVIEW", "executor handoff에는 명시적인 approval_required=true가 필요합니다."
    return route.status, route.reason


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralExecutorHandoff:
    """Immutable, non-authoritative handoff envelope for one bounded context."""

    context_result: StructuralReportContextResult
    action_request: ActionRequest
    route: ActionRoute
    status: str
    reason: str
    stages: tuple[str, ...] = STRUCTURAL_EXECUTOR_HANDOFF_STAGES
    schema_version: str = STRUCTURAL_EXECUTOR_HANDOFF_SCHEMA
    id: str = ""
    automatic: bool = False
    dispatch_performed: bool = False
    execution_allowed: bool = False
    filesystem_mutation: bool = False
    source_mutation: bool = False
    workspace_mutation: bool = False
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_EXECUTOR_HANDOFF_SCHEMA:
            raise StructuralExecutorHandoffError(
                "지원하지 않는 structural executor handoff schema입니다."
            )
        if not isinstance(self.context_result, StructuralReportContextResult):
            raise StructuralExecutorHandoffError("context_result 타입이 잘못되었습니다.")
        if not isinstance(self.action_request, ActionRequest):
            raise StructuralExecutorHandoffError("action_request 타입이 잘못되었습니다.")
        if not isinstance(self.route, ActionRoute):
            raise StructuralExecutorHandoffError("route 타입이 잘못되었습니다.")
        if self.route.request != self.action_request:
            raise StructuralExecutorHandoffError("route와 action_request identity가 다릅니다.")
        if not isinstance(self.action_request.approval_required, bool) or not isinstance(
            self.action_request.approved, bool
        ):
            raise StructuralExecutorHandoffError("ActionRequest approval 상태가 잘못되었습니다.")
        stages = tuple(self.stages)
        if stages != STRUCTURAL_EXECUTOR_HANDOFF_STAGES:
            raise StructuralExecutorHandoffError("executor handoff 단계 순서가 계약과 다릅니다.")
        object.__setattr__(self, "stages", stages)
        if self.status not in STRUCTURAL_EXECUTOR_HANDOFF_STATUSES:
            raise StructuralExecutorHandoffError("executor handoff status가 잘못되었습니다.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise StructuralExecutorHandoffError("executor handoff reason이 필요합니다.")
        expected_status, expected_reason = _expected_status_and_reason(
            self.context_result,
            self.action_request,
            self.route,
        )
        if self.status != expected_status:
            raise StructuralExecutorHandoffError(
                "executor handoff status가 context gate와 ActionRoute에 맞지 않습니다."
            )
        if self.reason != expected_reason:
            raise StructuralExecutorHandoffError("executor handoff reason이 입력 계약과 다릅니다.")
        for value, label in (
            (self.automatic, "handoff.automatic"),
            (self.dispatch_performed, "handoff.dispatch_performed"),
            (self.execution_allowed, "handoff.execution_allowed"),
            (self.filesystem_mutation, "handoff.filesystem_mutation"),
            (self.source_mutation, "handoff.source_mutation"),
            (self.workspace_mutation, "handoff.workspace_mutation"),
            (self.canonical_mutation, "handoff.canonical_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise StructuralExecutorHandoffError(f"{label}은(는) false여야 합니다.")

        expected_id = f"structural-executor-handoff:{canonical_hash(self._hash_payload()).removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralExecutorHandoffError("executor handoff id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "context_result_id": self.context_result.id,
            "context_id": self.context_result.context.id,
            "context_hash": self.context_result.context.context_hash,
            "action_request": self.action_request.to_record(),
            "route": self.route.to_record(),
            "status": self.status,
            "reason": self.reason,
            "stages": list(self.stages),
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": "structural_executor_handoff",
            "stages": list(self.stages),
            "verified_context": self.context_result.to_record(),
            "action_request": self.action_request.to_record(),
            "route": self.route.to_record(),
            "status": self.status,
            "reason": self.reason,
            "dispatch_performed": False,
            "execution_allowed": False,
            "automatic": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        import json

        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def prepare_structural_executor_handoff(
    context_result: StructuralReportContextResult,
    action_request: ActionRequest,
    *,
    router: ActionRouter | None = None,
) -> StructuralExecutorHandoff:
    """Route one explicit action request for a verified context without dispatch."""

    if not isinstance(context_result, StructuralReportContextResult):
        raise StructuralExecutorHandoffError(
            "context_result는 StructuralReportContextResult이어야 합니다."
        )
    if not isinstance(action_request, ActionRequest):
        raise StructuralExecutorHandoffError("action_request는 ActionRequest이어야 합니다.")
    if router is None:
        router = ActionRouter()
    if not isinstance(router, ActionRouter):
        raise StructuralExecutorHandoffError("router는 ActionRouter이어야 합니다.")
    route = router.route(action_request)
    status, reason = _expected_status_and_reason(context_result, action_request, route)
    return StructuralExecutorHandoff(
        context_result=context_result,
        action_request=action_request,
        route=route,
        status=status,
        reason=reason,
    )


__all__ = [
    "STRUCTURAL_EXECUTOR_HANDOFF_SCHEMA",
    "STRUCTURAL_EXECUTOR_HANDOFF_STAGES",
    "STRUCTURAL_EXECUTOR_HANDOFF_STATUSES",
    "StructuralExecutorHandoff",
    "StructuralExecutorHandoffError",
    "prepare_structural_executor_handoff",
]
