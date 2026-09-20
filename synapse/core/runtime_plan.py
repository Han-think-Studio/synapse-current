"""Explicit, transport-safe runtime dispatch planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from synapse.core.action import ActionRoute
from synapse.core.table import CognitiveTable
from synapse.runtime.contracts import PreparedRequest, RuntimeRequest
from synapse.runtime.registry import get_builtin_adapter


class RuntimePlanError(ValueError):
    """Raised when a runtime dispatch plan cannot be prepared."""


@dataclass(frozen=True, slots=True)
class RuntimeDispatchPlan:
    id: str
    route_id: str
    provider: str
    request: RuntimeRequest
    prepared: PreparedRequest | None
    status: str
    reason: str

    def __post_init__(self) -> None:
        if self.status not in {"READY", "REVIEW", "BLOCKED"}:
            raise RuntimePlanError("RuntimeDispatchPlan status가 잘못되었습니다.")

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "route_id": self.route_id,
            "provider": self.provider,
            "request_id": self.request.request_id,
            "model": self.request.model,
            "prepared_url": self.prepared.url if self.prepared else None,
            "status": self.status,
            "reason": self.reason,
            "network_dispatched": False,
        }


def prepare_runtime_plan(
    table: CognitiveTable,
    route: ActionRoute,
    *,
    model: str = "local-model",
) -> RuntimeDispatchPlan:
    """Prepare a provider request but never send it."""
    if not model.strip():
        raise RuntimePlanError("runtime model이 비어 있습니다.")
    table_payload = json.dumps(table.to_record(), ensure_ascii=False, sort_keys=True, indent=2)
    request = RuntimeRequest(
        request_id=route.request.id,
        model=model,
        messages=(
            {
                "role": "system",
                "content": "Return structured candidate claims with evidence and unresolved items. Never write Canonical State.",
            },
            {"role": "user", "content": table_payload},
        ),
        parameters={"temperature": 0.2},
        created_at=f"action:{route.request.id}",
    )
    selected = route.selected
    if selected is None:
        provider = "none"
        prepared = None
        status = "BLOCKED"
        reason = route.reason
    elif selected.provider in {"ollama", "lmstudio"}:
        provider = selected.provider
        adapter = get_builtin_adapter(provider)
        if adapter is None:  # pragma: no cover - registry invariant
            raise RuntimePlanError("내장 runtime adapter를 찾지 못했습니다.")
        prepared = adapter.prepare(request)
        status = route.status
        reason = route.reason
    elif selected.provider in {"codex", "claude_code"}:
        provider = selected.provider
        prepared = None
        status = "REVIEW"
        reason = "external worker는 명시적 승인 전 metadata plan으로만 남깁니다."
    else:
        provider = selected.provider
        prepared = None
        status = route.status
        reason = route.reason
    canonical = json.dumps(
        {"route": route.to_record(), "request_id": request.request_id, "provider": provider, "table_id": table.id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return RuntimeDispatchPlan(
        id=f"runtime:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        route_id=route.request.id,
        provider=provider,
        request=request,
        prepared=prepared,
        status=status,
        reason=reason,
    )


__all__ = ["RuntimeDispatchPlan", "RuntimePlanError", "prepare_runtime_plan"]
