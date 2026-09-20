"""Capability- and policy-driven executor routing."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.runtime.contracts import DATA_EGRESS_POLICIES, NETWORK_SCOPES


class ActionRoutingError(ValueError):
    """Raised when an action request or executor catalog is invalid."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RoutingObservation:
    """Ephemeral facts used to rank an executor; never persisted as health."""

    available: bool = True
    quota_remaining: float | None = 100.0
    cost: float = 0.0
    context_fit: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise ActionRoutingError("routing availability는 boolean이어야 합니다.")
        if self.quota_remaining is not None and not 0 <= self.quota_remaining <= 100:
            raise ActionRoutingError("routing quota는 0..100 범위여야 합니다.")
        if self.cost < 0 or self.context_fit < 0:
            raise ActionRoutingError("routing cost/context_fit은 음수일 수 없습니다.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RoutingPolicy:
    """Deterministic ranking weights for capability-qualified executors."""

    cost_weight: float = 1.0
    context_fit_weight: float = 1.0

    def __post_init__(self) -> None:
        if self.cost_weight < 0 or self.context_fit_weight < 0:
            raise ActionRoutingError("routing policy 가중치는 음수일 수 없습니다.")


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    id: str
    provider: str
    capabilities: tuple[str, ...]
    local: bool
    priority: int
    network_scope: str = ""
    data_egress: str = ""

    @property
    def provider_quota_required(self) -> bool:
        """Only an explicitly offline deterministic host adapter has no provider quota."""

        return not (
            self.provider == "python" and self.local
            and self.network_scope == "none" and self.data_egress == "prohibited"
            and bool(self.capabilities)
            and set(self.capabilities) <= {"deterministic", "parse", "verify", "scaffold", "inspect_artifact"}
        )

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.provider.strip() or self.priority < 0:
            raise ActionRoutingError("ExecutorSpec id/provider/priority가 잘못되었습니다.")
        derived_network = "loopback" if self.local else "remote"
        derived_egress = "local_only" if self.local else "external_allowed"
        network = self.network_scope or derived_network
        egress = self.data_egress or derived_egress
        if network not in NETWORK_SCOPES or egress not in DATA_EGRESS_POLICIES:
            raise ActionRoutingError("ExecutorSpec locality/egress 정책이 잘못되었습니다.")
        object.__setattr__(self, "network_scope", network)
        object.__setattr__(self, "data_egress", egress)
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provider": self.provider,
            "capabilities": list(self.capabilities),
            "local": self.local,
            "priority": self.priority,
            "network_scope": self.network_scope,
            "data_egress": self.data_egress,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionRequest:
    id: str
    task_kind: str
    required_capabilities: tuple[str, ...] = ()
    privacy: str = "internal"
    approval_required: bool = False
    approved: bool = False
    attributes: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.task_kind.strip():
            raise ActionRoutingError("ActionRequest id/task_kind가 필요합니다.")
        if self.privacy not in {"internal", "local_only", "external_allowed"}:
            raise ActionRoutingError(f"지원하지 않는 privacy 정책입니다: {self.privacy}")
        if self.approval_required and not isinstance(self.approved, bool):
            raise ActionRoutingError("approval 상태가 잘못되었습니다.")
        object.__setattr__(self, "required_capabilities", tuple(sorted(set(self.required_capabilities))))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    @property
    def dispatch_allowed(self) -> bool:
        return not self.approval_required or self.approved

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "task_kind": self.task_kind,
            "required_capabilities": list(self.required_capabilities),
            "privacy": self.privacy,
            "approval_required": self.approval_required,
            "approved": self.approved,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class ActionRoute:
    request: ActionRequest
    selected: ExecutorSpec | None
    candidates: tuple[ExecutorSpec, ...]
    status: str
    reason: str
    decision: str = "selected"

    def __post_init__(self) -> None:
        if self.status not in {"READY", "REVIEW", "BLOCKED"}:
            raise ActionRoutingError("ActionRoute status가 잘못되었습니다.")
        object.__setattr__(self, "candidates", tuple(sorted(self.candidates, key=lambda item: (item.priority, item.id))))

    def to_record(self) -> dict[str, object]:
        return {
            "request": self.request.to_record(),
            "selected": self.selected.to_record() if self.selected else None,
            "candidates": [candidate.to_record() for candidate in self.candidates],
            "status": self.status,
            "reason": self.reason,
            "decision": self.decision,
            "dispatch_performed": False,
        }


class ActionRouter:
    """Select a worker resource; never dispatches it."""

    DEFAULT_EXECUTORS = (
        ExecutorSpec(
            id="python",
            provider="python",
            capabilities=("deterministic", "parse", "verify", "scaffold"),
            local=True,
            priority=0,
        ),
        ExecutorSpec(
            id="lmstudio",
            provider="lmstudio",
            capabilities=("analysis", "llm", "local_model"),
            local=True,
            priority=10,
        ),
        ExecutorSpec(
            id="ollama",
            provider="ollama",
            capabilities=("analysis", "llm", "local_model"),
            local=True,
            priority=11,
        ),
        ExecutorSpec(
            id="codex",
            provider="codex",
            capabilities=("analysis", "code", "external_worker"),
            local=False,
            priority=20,
        ),
        ExecutorSpec(
            id="claude_code",
            provider="claude_code",
            capabilities=("analysis", "code", "external_worker"),
            local=False,
            priority=21,
        ),
    )

    def __init__(self, executors: tuple[ExecutorSpec, ...] | None = None) -> None:
        catalog = tuple(executors or self.DEFAULT_EXECUTORS)
        if len({executor.id for executor in catalog}) != len(catalog):
            raise ActionRoutingError("Executor id가 중복됩니다.")
        self.executors = tuple(sorted(catalog, key=lambda item: (item.priority, item.id)))

    def route(
        self,
        request: ActionRequest,
        *,
        observations: Mapping[str, RoutingObservation] | None = None,
        policy: RoutingPolicy | None = None,
        canonical_registry: Any | None = None,
    ) -> ActionRoute:
        observations = observations or {}
        policy = policy or RoutingPolicy()
        canonical_reason = _canonical_binding_failure(request, canonical_registry)
        if canonical_reason is not None:
            return ActionRoute(
                request=request,
                selected=None,
                candidates=(),
                status="BLOCKED",
                reason=canonical_reason,
            )
        required = set(request.required_capabilities)
        candidates = tuple(
            executor
            for executor in self.executors
            if required.issubset(executor.capabilities)
            and (request.privacy in {"external_allowed"} or executor.local)
            and observations.get(executor.id, RoutingObservation()).available
            and (
                (not executor.provider_quota_required and executor.id in observations)
                or (
                    executor.provider_quota_required
                    and observations.get(executor.id, RoutingObservation()).quota_remaining is not None
                    and observations.get(executor.id, RoutingObservation()).quota_remaining > 0
                )
            )
        )
        if not candidates:
            return ActionRoute(
                request=request,
                selected=None,
                candidates=(),
                status="BLOCKED",
                reason="요구 capability와 privacy 정책을 만족하는 executor가 없습니다.",
            )
        selected = min(
            candidates,
            key=lambda executor: (
                policy.cost_weight * observations.get(executor.id, RoutingObservation()).cost
                - policy.context_fit_weight * observations.get(executor.id, RoutingObservation()).context_fit,
                executor.priority,
                executor.id,
            ),
        )
        if not request.dispatch_allowed:
            status = "REVIEW"
            reason = "approval 전에는 executor를 dispatch하지 않습니다."
        else:
            status = "READY"
            reason = f"capability/정책에 따라 {selected.id}를 선택했습니다."
        return ActionRoute(
            request=request,
            selected=selected,
            candidates=candidates,
            status=status,
            reason=reason,
        )


def _canonical_binding_failure(
    request: ActionRequest,
    canonical_registry: Any | None,
) -> str | None:
    """Fail closed for requests explicitly bound to Canonical State.

    Ordinary ActionRequest routing stays provider-neutral.  Project-bound
    requests opt into this check through their existing attributes, so the
    router does not become a second registry or a new authority.
    """

    attributes = request.attributes
    if "canonical_revision_required" not in attributes:
        return None
    if attributes.get("canonical_revision_required") is not True:
        return "project-bound ActionRequest의 Canonical revision binding이 잘못되었습니다."
    if canonical_registry is None:
        return "Canonical Registry 확인 없이 project-bound action을 route할 수 없습니다."

    project_id = attributes.get("project_id")
    expected_revision = attributes.get("canonical_revision")
    if not isinstance(project_id, str) or not project_id.strip():
        return "project-bound ActionRequest에 Canonical project id가 없습니다."
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        return "project-bound ActionRequest에 Canonical revision이 없습니다."
    expected_item_hash = attributes.get("canonical_item_hash")
    if not isinstance(expected_item_hash, str) or not expected_item_hash.strip():
        return "project-bound ActionRequest에 Canonical item fingerprint가 없습니다."

    get_item = getattr(canonical_registry, "get", None)
    current_revision = getattr(canonical_registry, "revision", None)
    if not callable(get_item) or not isinstance(current_revision, int):
        return "유효한 Canonical Registry가 필요합니다."
    current = get_item(project_id)
    if current is None:
        return "요청에 묶인 Canonical project가 Registry에 없습니다."
    if current_revision != expected_revision:
        return "Canonical project가 요청 생성 뒤 변경되어 stale action을 route할 수 없습니다."
    try:
        current_item_hash = canonical_hash(current)
    except (TypeError, ValueError):
        return "Canonical project fingerprint를 확인할 수 없습니다."
    if current_item_hash != expected_item_hash:
        return "Canonical project 내용이 요청 binding과 달라 stale action을 route할 수 없습니다."
    current_attributes = getattr(current, "attributes", None)
    if not isinstance(current_attributes, Mapping):
        return "Canonical project action binding의 attributes를 확인할 수 없습니다."
    if attributes.get("entrypoint") != current_attributes.get("entrypoint"):
        return "ActionRequest entrypoint가 Canonical project binding과 다릅니다."
    if "project_source_id" in attributes and attributes.get("project_source_id") != current_attributes.get("source_id"):
        return "ActionRequest source가 Canonical project binding과 다릅니다."
    return None


def route_table_action(
    table_id: str,
    *,
    task_kind: str = "table_review",
    privacy: str = "internal",
    approved: bool = False,
) -> ActionRoute:
    """Convenience boundary from a Cognitive Table id to a local-first route."""
    request = ActionRequest(
        id=f"action:{hashlib.sha256(table_id.encode('utf-8')).hexdigest()}",
        task_kind=task_kind,
        required_capabilities=("analysis",),
        privacy=privacy,
        approval_required=True,
        approved=bool(approved),
        attributes={"table_id": table_id},
    )
    return ActionRouter().route(request)


__all__ = ["ActionRequest", "ActionRoute", "ActionRouter", "ActionRoutingError", "ExecutorSpec", "RoutingObservation", "RoutingPolicy", "route_table_action"]
