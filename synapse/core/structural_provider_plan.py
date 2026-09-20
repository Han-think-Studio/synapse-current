"""Prepare a provider request from a structural runtime plan.

Phase 54 creates a provider-neutral ``RuntimeRequest``.  This module reuses
the explicitly supplied local adapter's pure ``prepare`` method to produce one
``PreparedRequest`` in memory.  It does not send the request or call any
provider, model, executor, or network service.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_runtime_plan import StructuralRuntimeRequestPlan
from synapse.runtime.contracts import PreparedRequest, is_runtime_adapter
from synapse.runtime.policy import (
    RuntimePolicyError,
    network_scope_for_url,
    validate_endpoint_policy,
)

STRUCTURAL_PROVIDER_PLAN_SCHEMA = "structural.provider.request.plan.v1"
STRUCTURAL_PROVIDER_PLAN_STAGES = (
    "RUNTIME_PLAN_VERIFIED",
    "PROVIDER_REQUEST_PREPARED",
)
MAX_STRUCTURAL_PROVIDER_BODY_BYTES = 1024 * 1024
_PLAN_REASON = "provider request를 memory-only로 준비했습니다."


class StructuralProviderPlanError(ValueError):
    """Raised when a provider request cannot be prepared safely."""


def _text(value: Any, label: str, *, limit: int = 4_000) -> str:
    if not isinstance(value, str):
        raise StructuralProviderPlanError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if not result:
        raise StructuralProviderPlanError(f"{label}은(는) 비어 있을 수 없습니다.")
    if "\x00" in result:
        raise StructuralProviderPlanError(f"{label}에 허용되지 않은 NUL 문자가 있습니다.")
    if len(result) > limit:
        raise StructuralProviderPlanError(f"{label}이(가) 너무 깁니다.")
    return result


def _prepared_record(prepared: PreparedRequest) -> dict[str, Any]:
    body = dict(prepared.body)
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    sensitive_headers = {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
    }
    return {
        "request_id": prepared.request_id,
        "provider": prepared.provider,
        "method": prepared.method,
        "url": prepared.url,
        "header_names": sorted(str(key).lower() for key in prepared.headers),
        "secret_present": any(
            str(key).lower() in sensitive_headers for key in prepared.headers
        ),
        "body_sha256": canonical_hash(body),
        "body_bytes": len(encoded),
    }


def _runtime_plan_record(runtime_plan: StructuralRuntimeRequestPlan) -> dict[str, Any]:
    """Project a runtime plan without serializing prompt content."""

    record = runtime_plan.to_record()
    request = dict(record["runtime_request"])
    messages = request.pop("messages")
    encoded = json.dumps(
        messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    request["message_count"] = len(messages)
    request["messages_sha256"] = canonical_hash(messages)
    request["messages_bytes"] = len(encoded)
    record["runtime_request"] = request
    return record


def _validate_prepared_request(
    runtime_plan: StructuralRuntimeRequestPlan,
    prepared: PreparedRequest,
) -> tuple[str, str]:
    if not isinstance(prepared, PreparedRequest):
        raise StructuralProviderPlanError("adapter는 PreparedRequest를 반환해야 합니다.")
    provider = _text(prepared.provider, "prepared.provider", limit=160).lower()
    endpoint = _text(prepared.url, "prepared.url", limit=4_096)
    if provider != runtime_plan.provider:
        raise StructuralProviderPlanError("PreparedRequest provider가 runtime plan과 다릅니다.")
    if prepared.request_id != runtime_plan.runtime_request.request_id:
        raise StructuralProviderPlanError("PreparedRequest request identity가 runtime plan과 다릅니다.")
    _text(prepared.method, "prepared.method", limit=40)
    if not isinstance(prepared.headers, Mapping) or not isinstance(prepared.body, Mapping):
        raise StructuralProviderPlanError("PreparedRequest headers/body는 mapping이어야 합니다.")
    body = dict(prepared.body)
    if prepared.method == "BROKER":
        if provider != "antigravity" or body.get("operation") != "antigravity.invoke":
            raise StructuralProviderPlanError("BROKER PreparedRequest operation이 허용되지 않았습니다.")
        if body.get("task_id") != runtime_plan.runtime_request.request_id:
            raise StructuralProviderPlanError("BROKER task_id가 runtime request와 다릅니다.")
        if body.get("model") != runtime_plan.model or not isinstance(body.get("prompt"), str):
            raise StructuralProviderPlanError("BROKER request가 bounded runtime request와 다릅니다.")
    else:
        if body.get("model") != runtime_plan.model:
            raise StructuralProviderPlanError("PreparedRequest model이 runtime plan과 다릅니다.")
        expected_messages = [dict(message) for message in runtime_plan.runtime_request.messages]
        if body.get("messages") != expected_messages:
            raise StructuralProviderPlanError("PreparedRequest messages가 bounded request와 다릅니다.")
    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StructuralProviderPlanError("PreparedRequest body는 유효한 JSON이어야 합니다.") from exc
    if len(encoded) > MAX_STRUCTURAL_PROVIDER_BODY_BYTES:
        raise StructuralProviderPlanError(
            f"PreparedRequest body가 허용 크기({MAX_STRUCTURAL_PROVIDER_BODY_BYTES} bytes)를 초과했습니다."
        )
    privacy = runtime_plan.handoff.action_request.privacy
    try:
        actual_scope = network_scope_for_url(endpoint)
        if actual_scope == "none":
            declared_scope = "none"
            declared_egress = "external_allowed"
        else:
            declared_scope = "remote" if privacy == "external_allowed" else "loopback"
            declared_egress = "external_allowed" if privacy == "external_allowed" else "local_only"
        validate_endpoint_policy(
            endpoint,
            network_scope=declared_scope,
            data_egress=declared_egress,
        )
    except RuntimePolicyError as exc:
        raise StructuralProviderPlanError(str(exc)) from exc
    return provider, endpoint


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralProviderRequestPlan:
    """Immutable, memory-only PreparedRequest preparation result."""

    runtime_plan: StructuralRuntimeRequestPlan
    provider: str
    endpoint: str
    prepared_request: PreparedRequest
    status: str = "READY"
    reason: str = _PLAN_REASON
    stages: tuple[str, ...] = STRUCTURAL_PROVIDER_PLAN_STAGES
    schema_version: str = STRUCTURAL_PROVIDER_PLAN_SCHEMA
    id: str = ""
    network_dispatched: bool = False
    dispatch_performed: bool = False
    execution_allowed: bool = False
    automatic: bool = False
    filesystem_mutation: bool = False
    source_mutation: bool = False
    workspace_mutation: bool = False
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_PROVIDER_PLAN_SCHEMA:
            raise StructuralProviderPlanError("지원하지 않는 structural provider plan schema입니다.")
        if not isinstance(self.runtime_plan, StructuralRuntimeRequestPlan):
            raise StructuralProviderPlanError("runtime_plan 타입이 잘못되었습니다.")
        if self.runtime_plan.status != "READY":
            raise StructuralProviderPlanError("READY runtime plan에서만 provider request를 준비할 수 있습니다.")
        provider, endpoint = _validate_prepared_request(self.runtime_plan, self.prepared_request)
        object.__setattr__(self, "provider", provider)
        if self.endpoint != endpoint:
            raise StructuralProviderPlanError("plan endpoint가 PreparedRequest와 다릅니다.")
        if self.status != "READY":
            raise StructuralProviderPlanError("structural provider plan은 READY여야 합니다.")
        if self.reason != _PLAN_REASON:
            raise StructuralProviderPlanError("provider plan reason이 계약과 다릅니다.")
        stages = tuple(self.stages)
        if stages != STRUCTURAL_PROVIDER_PLAN_STAGES:
            raise StructuralProviderPlanError("provider plan 단계 순서가 계약과 다릅니다.")
        object.__setattr__(self, "stages", stages)
        for value, label in (
            (self.network_dispatched, "plan.network_dispatched"),
            (self.dispatch_performed, "plan.dispatch_performed"),
            (self.execution_allowed, "plan.execution_allowed"),
            (self.automatic, "plan.automatic"),
            (self.filesystem_mutation, "plan.filesystem_mutation"),
            (self.source_mutation, "plan.source_mutation"),
            (self.workspace_mutation, "plan.workspace_mutation"),
            (self.canonical_mutation, "plan.canonical_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise StructuralProviderPlanError(f"{label}은(는) false여야 합니다.")

        expected_id = f"structural-provider-plan:{canonical_hash(self._hash_payload()).removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralProviderPlanError("provider plan id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_plan_id": self.runtime_plan.id,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "prepared_request": _prepared_record(self.prepared_request),
            "status": self.status,
            "reason": self.reason,
            "stages": list(self.stages),
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": "structural_provider_request_plan",
            "stages": list(self.stages),
            "runtime_plan": _runtime_plan_record(self.runtime_plan),
            "provider": self.provider,
            "endpoint": self.endpoint,
            "status": self.status,
            "reason": self.reason,
            "prepared_request": _prepared_record(self.prepared_request),
            "network_dispatched": False,
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


def prepare_structural_provider_request(
    runtime_plan: StructuralRuntimeRequestPlan,
    *,
    adapter: object,
) -> StructuralProviderRequestPlan:
    """Prepare one provider request through an explicitly supplied adapter."""

    if not isinstance(runtime_plan, StructuralRuntimeRequestPlan):
        raise StructuralProviderPlanError(
            "runtime_plan은 StructuralRuntimeRequestPlan이어야 합니다."
        )
    if runtime_plan.status != "READY":
        raise StructuralProviderPlanError("READY runtime plan에서만 provider request를 준비할 수 있습니다.")
    if not is_runtime_adapter(adapter):
        raise StructuralProviderPlanError(
            "LMStudioAdapter, OllamaAdapter 또는 RuntimeAdapter 계약을 만족하는 adapter를 명시적으로 제공해야 합니다."
        )
    adapter_provider = _text(adapter.provider, "adapter.provider", limit=160).lower()
    if adapter_provider != runtime_plan.provider:
        raise StructuralProviderPlanError("adapter provider가 runtime plan provider와 다릅니다.")
    try:
        prepared = adapter.prepare(runtime_plan.runtime_request)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise StructuralProviderPlanError(f"provider request 준비에 실패했습니다: {exc}") from exc
    provider, endpoint = _validate_prepared_request(runtime_plan, prepared)
    return StructuralProviderRequestPlan(
        runtime_plan=runtime_plan,
        provider=provider,
        endpoint=endpoint,
        prepared_request=prepared,
    )


__all__ = [
    "MAX_STRUCTURAL_PROVIDER_BODY_BYTES",
    "STRUCTURAL_PROVIDER_PLAN_SCHEMA",
    "STRUCTURAL_PROVIDER_PLAN_STAGES",
    "StructuralProviderPlanError",
    "StructuralProviderRequestPlan",
    "prepare_structural_provider_request",
]
