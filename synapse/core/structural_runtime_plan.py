"""Prepare a provider-neutral runtime request from a verified handoff.

This module is intentionally one step before a provider adapter.  It accepts
only a READY Phase 53 handoff and a caller-supplied model name, then creates
the existing immutable ``RuntimeRequest`` in memory.  It does not create a
``PreparedRequest`` or invoke any provider, model, executor, or network path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from synapse.core.idea_session import canonical_hash, canonical_json
from synapse.core.structural_executor_handoff import StructuralExecutorHandoff
from synapse.runtime.contracts import RuntimeRequest

STRUCTURAL_RUNTIME_PLAN_SCHEMA = "structural.runtime.plan.v1"
STRUCTURAL_RUNTIME_PLAN_STAGES = (
    "HANDOFF_VERIFIED",
    "RUNTIME_REQUEST_PREPARED",
)
MAX_STRUCTURAL_RUNTIME_MESSAGE_BYTES = 512 * 1024
_SYSTEM_MESSAGE = (
    "This is a bounded structural context for review. It is evidence only, "
    "not Canonical State or execution approval. Preserve unresolved items, "
    "do not invent relationships, and return a proposal without writing files "
    "or state."
)


class StructuralRuntimePlanError(ValueError):
    """Raised when a provider-neutral runtime request cannot be prepared."""


def _text(value: Any, label: str, *, limit: int = 4_000) -> str:
    if not isinstance(value, str):
        raise StructuralRuntimePlanError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if not result:
        raise StructuralRuntimePlanError(f"{label}은(는) 비어 있을 수 없습니다.")
    if "\x00" in result:
        raise StructuralRuntimePlanError(f"{label}에 허용되지 않은 NUL 문자가 있습니다.")
    if len(result) > limit:
        raise StructuralRuntimePlanError(f"{label}이(가) 너무 깁니다.")
    return result


def _parameters(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise StructuralRuntimePlanError("runtime parameters는 mapping이어야 합니다.")
    return dict(value)


def _lineage(handoff: StructuralExecutorHandoff) -> dict[str, Any]:
    context_result = handoff.context_result
    report = context_result.inspection.report
    review = report.review
    observation = review.observation
    context = context_result.context
    return {
        "handoff_id": handoff.id,
        "context_result_id": context_result.id,
        "inspection_id": context_result.inspection.id,
        "report_id": report.id,
        "review_id": review.id,
        "observation_id": observation.id,
        "observation_hash": observation.observation_hash,
        "context_id": context.id,
        "context_hash": context.context_hash,
        "source_id": context.source_id,
        "source_hash": context.source_hash,
        "input_sha256": review.input_sha256,
    }


def _prompt_payload(handoff: StructuralExecutorHandoff) -> dict[str, Any]:
    report = handoff.context_result.inspection.report
    return {
        "lineage": _lineage(handoff),
        "structural_gate": {
            "status": handoff.context_result.report_gate_status,
            "passed": handoff.context_result.report_gate_passed,
            "exit_code": handoff.context_result.report_gate_exit_code,
        },
        "action_request": handoff.action_request.to_record(),
        "route": handoff.route.to_record(),
        "context": handoff.context_result.context.to_record(),
        "report_context_schema": handoff.context_result.schema_version,
        "report_schema": report.schema_version,
    }


def _messages(handoff: StructuralExecutorHandoff) -> tuple[dict[str, str], dict[str, str]]:
    user_content = canonical_json(_prompt_payload(handoff))
    if len(user_content.encode("utf-8")) > MAX_STRUCTURAL_RUNTIME_MESSAGE_BYTES:
        raise StructuralRuntimePlanError(
            f"bounded context message가 허용 크기({MAX_STRUCTURAL_RUNTIME_MESSAGE_BYTES} bytes)를 초과했습니다."
        )
    return (
        {"role": "system", "content": _SYSTEM_MESSAGE},
        {"role": "user", "content": user_content},
    )


def _request_id(
    handoff: StructuralExecutorHandoff,
    model: str,
    messages: tuple[dict[str, str], dict[str, str]],
    parameters: Mapping[str, Any],
) -> str:
    payload = {
        "handoff_id": handoff.id,
        "model": model,
        "messages": messages,
        "parameters": dict(parameters),
    }
    return f"structural-runtime-request:{canonical_hash(payload).removeprefix('sha256:')}"


def _runtime_request_record(request: RuntimeRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "model": request.model,
        "messages": [dict(message) for message in request.messages],
        "parameters": dict(request.parameters),
        "created_at": request.created_at,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralRuntimeRequestPlan:
    """Immutable, provider-neutral RuntimeRequest preparation result."""

    handoff: StructuralExecutorHandoff
    provider: str
    model: str
    runtime_request: RuntimeRequest
    status: str = "READY"
    reason: str = "provider-neutral RuntimeRequest를 준비했습니다."
    stages: tuple[str, ...] = STRUCTURAL_RUNTIME_PLAN_STAGES
    schema_version: str = STRUCTURAL_RUNTIME_PLAN_SCHEMA
    id: str = ""
    automatic: bool = False
    dispatch_performed: bool = False
    execution_allowed: bool = False
    filesystem_mutation: bool = False
    source_mutation: bool = False
    workspace_mutation: bool = False
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_RUNTIME_PLAN_SCHEMA:
            raise StructuralRuntimePlanError("지원하지 않는 structural runtime plan schema입니다.")
        if not isinstance(self.handoff, StructuralExecutorHandoff):
            raise StructuralRuntimePlanError("handoff 타입이 잘못되었습니다.")
        if self.handoff.status != "READY":
            raise StructuralRuntimePlanError("READY handoff에서만 RuntimeRequest를 준비할 수 있습니다.")
        if not isinstance(self.runtime_request, RuntimeRequest):
            raise StructuralRuntimePlanError("runtime_request 타입이 잘못되었습니다.")
        provider = _text(self.provider, "plan.provider", limit=160).lower()
        model = _text(self.model, "plan.model", limit=240)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        selected = self.handoff.route.selected
        if selected is None or selected.provider.strip().lower() != provider:
            raise StructuralRuntimePlanError("plan provider가 handoff route와 다릅니다.")
        if self.status != "READY":
            raise StructuralRuntimePlanError("structural runtime plan은 READY여야 합니다.")
        if self.reason != "provider-neutral RuntimeRequest를 준비했습니다.":
            raise StructuralRuntimePlanError("runtime plan reason이 계약과 다릅니다.")
        stages = tuple(self.stages)
        if stages != STRUCTURAL_RUNTIME_PLAN_STAGES:
            raise StructuralRuntimePlanError("runtime plan 단계 순서가 계약과 다릅니다.")
        object.__setattr__(self, "stages", stages)

        messages = _messages(self.handoff)
        parameters = dict(self.runtime_request.parameters)
        expected_request_id = _request_id(self.handoff, model, messages, parameters)
        if self.runtime_request.request_id != expected_request_id:
            raise StructuralRuntimePlanError("RuntimeRequest identity가 handoff/model과 다릅니다.")
        if self.runtime_request.model != model:
            raise StructuralRuntimePlanError("RuntimeRequest model이 plan과 다릅니다.")
        if tuple(dict(message) for message in self.runtime_request.messages) != messages:
            raise StructuralRuntimePlanError("RuntimeRequest message가 bounded context와 다릅니다.")
        if self.runtime_request.created_at != f"handoff:{self.handoff.id}":
            raise StructuralRuntimePlanError("RuntimeRequest created_at이 handoff에 묶여 있지 않습니다.")
        for value, label in (
            (self.automatic, "plan.automatic"),
            (self.dispatch_performed, "plan.dispatch_performed"),
            (self.execution_allowed, "plan.execution_allowed"),
            (self.filesystem_mutation, "plan.filesystem_mutation"),
            (self.source_mutation, "plan.source_mutation"),
            (self.workspace_mutation, "plan.workspace_mutation"),
            (self.canonical_mutation, "plan.canonical_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise StructuralRuntimePlanError(f"{label}은(는) false여야 합니다.")

        expected_id = f"structural-runtime-plan:{canonical_hash(self._hash_payload()).removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralRuntimePlanError("runtime plan id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "handoff_id": self.handoff.id,
            "provider": self.provider,
            "model": self.model,
            "request": _runtime_request_record(self.runtime_request),
            "status": self.status,
            "reason": self.reason,
            "stages": list(self.stages),
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": "structural_runtime_request_plan",
            "stages": list(self.stages),
            "handoff_id": self.handoff.id,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "reason": self.reason,
            "runtime_request": _runtime_request_record(self.runtime_request),
            "lineage": _lineage(self.handoff),
            "dispatch_performed": False,
            "execution_allowed": False,
            "automatic": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def prepare_structural_runtime_request(
    handoff: StructuralExecutorHandoff,
    *,
    model: str,
    parameters: Mapping[str, Any] | None = None,
) -> StructuralRuntimeRequestPlan:
    """Prepare one explicit RuntimeRequest without creating provider payload."""

    if not isinstance(handoff, StructuralExecutorHandoff):
        raise StructuralRuntimePlanError("handoff는 StructuralExecutorHandoff이어야 합니다.")
    if handoff.status != "READY":
        raise StructuralRuntimePlanError("READY handoff에서만 RuntimeRequest를 준비할 수 있습니다.")
    normalized_model = _text(model, "model", limit=240)
    normalized_parameters = _parameters(parameters)
    messages = _messages(handoff)
    selected = handoff.route.selected
    if selected is None:  # pragma: no cover - READY handoff prevents this
        raise StructuralRuntimePlanError("READY handoff에는 선택된 executor가 필요합니다.")
    provider = _text(selected.provider, "provider", limit=160).lower()
    request = RuntimeRequest(
        request_id=_request_id(handoff, normalized_model, messages, normalized_parameters),
        model=normalized_model,
        messages=messages,
        parameters=normalized_parameters,
        created_at=f"handoff:{handoff.id}",
    )
    return StructuralRuntimeRequestPlan(
        handoff=handoff,
        provider=provider,
        model=normalized_model,
        runtime_request=request,
    )


__all__ = [
    "MAX_STRUCTURAL_RUNTIME_MESSAGE_BYTES",
    "STRUCTURAL_RUNTIME_PLAN_SCHEMA",
    "STRUCTURAL_RUNTIME_PLAN_STAGES",
    "StructuralRuntimePlanError",
    "StructuralRuntimeRequestPlan",
    "prepare_structural_runtime_request",
]
