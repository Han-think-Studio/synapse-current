"""Perform one explicitly approved dispatch from a structural provider plan.

Phase 55 prepares a provider payload without sending it.  This module is the
next explicit boundary: it consumes that prepared payload through a caller-
supplied transport, normalizes the response with the caller-supplied existing
adapter, and reuses the shared RuntimeOperation receipt.  It never discovers
or rebuilds a provider request, retries, falls back, or mutates project state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_provider_plan import StructuralProviderRequestPlan
from synapse.runtime.contracts import RuntimeRequest, RuntimeResponse, is_runtime_adapter, utc_now
from synapse.runtime.http_transport import JsonTransport
from synapse.runtime.operation import (
    RUNTIME_OPERATION_STAGES,
    RuntimeOperationError,
    RuntimeOperationResult,
    run_explicit_runtime_operation,
)

STRUCTURAL_PROVIDER_DISPATCH_SCHEMA = "structural.provider.dispatch.v1"
STRUCTURAL_PROVIDER_DISPATCH_STAGES = (
    "PROVIDER_PLAN_VERIFIED",
    "REQUEST_VALIDATED",
    "DISPATCH_APPROVED",
    "PROVIDER_INVOKED",
    "RESPONSE_NORMALIZED",
)
STRUCTURAL_PROVIDER_DISPATCH_STATUSES = frozenset({"SUCCEEDED", "FAILED"})


class StructuralProviderDispatchError(ValueError):
    """Raised when an explicit structural provider dispatch is unsafe."""


def _lineage(plan: StructuralProviderRequestPlan) -> dict[str, Any]:
    runtime_plan = plan.runtime_plan
    handoff = runtime_plan.handoff
    context_result = handoff.context_result
    inspection = context_result.inspection
    report = inspection.report
    review = report.review
    observation = review.observation
    context = context_result.context
    return {
        "provider_plan_id": plan.id,
        "runtime_plan_id": runtime_plan.id,
        "handoff_id": handoff.id,
        "context_result_id": context_result.id,
        "inspection_id": inspection.id,
        "report_id": report.id,
        "review_id": review.id,
        "observation_id": observation.id,
        "observation_hash": observation.observation_hash,
        "workspace_hash": observation.workspace_hash,
        "context_id": context.id,
        "context_hash": context.context_hash,
        "source_id": context.source_id,
        "source_hash": context.source_hash,
        "input_sha256": review.input_sha256,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralProviderDispatchResult:
    """Immutable receipt for one explicit dispatch from a prepared plan."""

    provider_plan: StructuralProviderRequestPlan
    operation: RuntimeOperationResult
    status: str
    stages: tuple[str, ...]
    schema_version: str = STRUCTURAL_PROVIDER_DISPATCH_SCHEMA
    id: str = ""
    dispatch_approved: bool = True
    transport_invoked: bool = True
    provider_invoked: bool = True
    automatic: bool = False
    retry_count: int = 0
    fallback_used: bool = False
    filesystem_mutation: bool = False
    source_mutation: bool = False
    workspace_mutation: bool = False
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_PROVIDER_DISPATCH_SCHEMA:
            raise StructuralProviderDispatchError(
                "지원하지 않는 structural provider dispatch schema입니다."
            )
        if not isinstance(self.provider_plan, StructuralProviderRequestPlan):
            raise StructuralProviderDispatchError("provider_plan 타입이 잘못되었습니다.")
        if not isinstance(self.operation, RuntimeOperationResult):
            raise StructuralProviderDispatchError("operation 타입이 잘못되었습니다.")
        if self.provider_plan.status != "READY":
            raise StructuralProviderDispatchError("READY provider plan에서만 dispatch할 수 있습니다.")
        if self.operation.request != self.provider_plan.runtime_plan.runtime_request:
            raise StructuralProviderDispatchError("operation request가 provider plan과 다릅니다.")
        if self.operation.provider != self.provider_plan.provider:
            raise StructuralProviderDispatchError("operation provider가 provider plan과 다릅니다.")
        if self.operation.endpoint != self.provider_plan.endpoint:
            raise StructuralProviderDispatchError("operation endpoint가 provider plan과 다릅니다.")
        if self.status != self.operation.status:
            raise StructuralProviderDispatchError("dispatch status가 operation과 다릅니다.")
        if self.status not in STRUCTURAL_PROVIDER_DISPATCH_STATUSES:
            raise StructuralProviderDispatchError("dispatch status가 올바르지 않습니다.")
        expected_stages = (
            STRUCTURAL_PROVIDER_DISPATCH_STAGES
            if self.operation.succeeded
            else STRUCTURAL_PROVIDER_DISPATCH_STAGES[:-1]
        )
        stages = tuple(self.stages)
        if stages != expected_stages:
            raise StructuralProviderDispatchError("structural provider dispatch 단계 순서가 다릅니다.")
        object.__setattr__(self, "stages", stages)
        if self.operation.stages != RUNTIME_OPERATION_STAGES and self.operation.succeeded:
            raise StructuralProviderDispatchError("성공 operation 단계 순서가 다릅니다.")
        if self.operation.stages != RUNTIME_OPERATION_STAGES[:-1] and self.operation.failed:
            raise StructuralProviderDispatchError("실패 operation 단계 순서가 다릅니다.")
        for value, label in (
            (self.dispatch_approved, "dispatch.dispatch_approved"),
            (self.transport_invoked, "dispatch.transport_invoked"),
            (self.provider_invoked, "dispatch.provider_invoked"),
        ):
            if not isinstance(value, bool) or not value:
                raise StructuralProviderDispatchError(f"{label}은(는) true여야 합니다.")
        for value, label in (
            (self.automatic, "dispatch.automatic"),
            (self.fallback_used, "dispatch.fallback_used"),
            (self.filesystem_mutation, "dispatch.filesystem_mutation"),
            (self.source_mutation, "dispatch.source_mutation"),
            (self.workspace_mutation, "dispatch.workspace_mutation"),
            (self.canonical_mutation, "dispatch.canonical_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise StructuralProviderDispatchError(f"{label}은(는) false여야 합니다.")
        if not isinstance(self.retry_count, int) or self.retry_count != 0:
            raise StructuralProviderDispatchError("dispatch.retry_count는 0이어야 합니다.")
        if self.operation.retry_count != 0 or self.operation.fallback_used:
            raise StructuralProviderDispatchError("operation에 retry/fallback이 포함되어 있습니다.")
        expected_id = f"structural-provider-dispatch:{canonical_hash(self._hash_payload()).removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralProviderDispatchError("dispatch id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider_plan_id": self.provider_plan.id,
            "operation_id": self.operation.id,
            "provider": self.provider_plan.provider,
            "request_id": self.provider_plan.prepared_request.request_id,
            "model": self.provider_plan.runtime_plan.model,
            "endpoint": self.provider_plan.endpoint,
            "status": self.status,
            "stages": list(self.stages),
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": "structural_provider_dispatch_receipt",
            "provider_plan_id": self.provider_plan.id,
            "runtime_plan_id": self.provider_plan.runtime_plan.id,
            "provider": self.provider_plan.provider,
            "request_id": self.provider_plan.prepared_request.request_id,
            "model": self.provider_plan.runtime_plan.model,
            "endpoint": self.provider_plan.endpoint,
            "status": self.status,
            "stages": list(self.stages),
            "lineage": _lineage(self.provider_plan),
            "operation": self.operation.to_record(),
            "dispatch_approved": True,
            "transport_invoked": True,
            "provider_invoked": True,
            "automatic": False,
            "retry_count": 0,
            "fallback_used": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def dispatch_structural_provider_request(
    provider_plan: StructuralProviderRequestPlan,
    *,
    adapter: object,
    transport: JsonTransport,
    dispatch_approved: bool,
    clock: Any = utc_now,
) -> StructuralProviderDispatchResult:
    """Dispatch one prepared request through an explicitly supplied transport."""

    if not isinstance(provider_plan, StructuralProviderRequestPlan):
        raise StructuralProviderDispatchError(
            "provider_plan은 StructuralProviderRequestPlan이어야 합니다."
        )
    if provider_plan.status != "READY":
        raise StructuralProviderDispatchError("READY provider plan에서만 dispatch할 수 있습니다.")
    if dispatch_approved is not True:
        raise StructuralProviderDispatchError(
            "structural provider dispatch에는 dispatch_approved=true가 필요합니다."
        )
    if not is_runtime_adapter(adapter):
        raise StructuralProviderDispatchError(
            "LMStudioAdapter, OllamaAdapter 또는 RuntimeAdapter 계약을 만족하는 adapter를 명시적으로 제공해야 합니다."
        )
    if adapter.provider.strip().lower() != provider_plan.provider:
        raise StructuralProviderDispatchError("adapter provider가 provider plan과 다릅니다.")
    send = getattr(transport, "send", None)
    if not callable(send):
        raise StructuralProviderDispatchError("transport는 send()를 제공해야 합니다.")
    normalize = getattr(adapter, "normalize", None)
    if not callable(normalize):
        raise StructuralProviderDispatchError("adapter는 normalize()를 제공해야 합니다.")

    request = provider_plan.runtime_plan.runtime_request
    prepared = provider_plan.prepared_request

    def complete(received: RuntimeRequest) -> RuntimeResponse:
        if received != request:
            raise RuntimeOperationError("dispatch request identity가 provider plan과 다릅니다.")
        payload = send(prepared, timeout_seconds=received.timeout_seconds)
        if not isinstance(payload, Mapping):
            raise RuntimeOperationError("transport가 JSON object를 반환하지 않았습니다.")
        response = normalize(received, payload)
        if not isinstance(response, RuntimeResponse):
            raise RuntimeOperationError("adapter가 RuntimeResponse를 반환하지 않았습니다.")
        return response

    operation = run_explicit_runtime_operation(
        request,
        provider=provider_plan.provider,
        complete=complete,
        dispatch_approved=True,
        endpoint=provider_plan.endpoint,
        clock=clock,
    )
    stages = (
        STRUCTURAL_PROVIDER_DISPATCH_STAGES
        if operation.succeeded
        else STRUCTURAL_PROVIDER_DISPATCH_STAGES[:-1]
    )
    return StructuralProviderDispatchResult(
        provider_plan=provider_plan,
        operation=operation,
        status=operation.status,
        stages=stages,
    )


__all__ = [
    "STRUCTURAL_PROVIDER_DISPATCH_SCHEMA",
    "STRUCTURAL_PROVIDER_DISPATCH_STAGES",
    "STRUCTURAL_PROVIDER_DISPATCH_STATUSES",
    "StructuralProviderDispatchError",
    "StructuralProviderDispatchResult",
    "dispatch_structural_provider_request",
]
