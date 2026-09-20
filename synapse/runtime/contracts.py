"""Provider-neutral request, response, and observation contracts.

These types intentionally perform no network I/O.  A provider adapter may
prepare a request and normalize a response, while the caller remains
responsible for transport, validation, and any Canonical State proposal.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

EXECUTION_SCOPES = frozenset({"builtin", "process", "service", "remote"})
NETWORK_SCOPES = frozenset({"none", "loopback", "lan", "remote"})
DATA_EGRESS_POLICIES = frozenset({"prohibited", "local_only", "external_allowed"})
PROVIDER_HEALTH_STATES = frozenset({"READY", "CONSERVE", "RESERVE", "EXHAUSTED", "AUTH_REQUIRED", "NOT_INSTALLED", "COOLDOWN", "ERROR", "UNKNOWN"})


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _health_time(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthEvidence:
    owner: str
    source: str
    observed_at: str
    expires_at: str | None = None
    reliable: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not isinstance(self.source, str) or not self.owner.strip() or not self.source.strip():
            raise ValueError("health evidence owner/source are required")
        if not isinstance(self.reliable, bool):
            raise ValueError("reliable must be a boolean")  # noqa: TRY004 - health records retain their tested ValueError contract
        observed = _health_time(self.observed_at, "observed_at")
        if self.expires_at is not None and _health_time(self.expires_at, "expires_at") <= observed:
            raise ValueError("expires_at must be after observed_at")

    def to_record(self) -> dict[str, Any]:
        return {"owner": self.owner, "source": self.source, "observed_at": self.observed_at, "expires_at": self.expires_at, "reliable": self.reliable}


@dataclass(frozen=True, slots=True, kw_only=True)
class QuotaPool:
    provider: str
    account: str | None
    pool: str
    models: tuple[str, ...] = ()
    remaining_percent: float | None = None
    remaining_fraction: float | None = None
    usage_state: str | None = None
    reset_at: str | None = None
    window: str | None = None
    evidence: HealthEvidence | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.pool.strip():
            raise ValueError("quota pool provider/pool are required")
        if self.remaining_percent is not None and (isinstance(self.remaining_percent, bool) or not isinstance(self.remaining_percent, (int, float)) or not math.isfinite(self.remaining_percent) or not 0 <= self.remaining_percent <= 100):
            raise ValueError("remaining_percent must be between 0 and 100")
        if self.remaining_fraction is not None and (isinstance(self.remaining_fraction, bool) or not isinstance(self.remaining_fraction, (int, float)) or not math.isfinite(self.remaining_fraction) or not 0 <= self.remaining_fraction <= 1):
            raise ValueError("remaining_fraction must be between 0 and 1")
        if self.reset_at is not None:
            _health_time(self.reset_at, "reset_at")
        object.__setattr__(self, "models", tuple(sorted({m.strip() for m in self.models if isinstance(m, str) and m.strip()})))

    def to_record(self) -> dict[str, Any]:
        return {"provider": self.provider, "account": self.account, "pool": self.pool, "models": list(self.models), "remaining_percent": self.remaining_percent, "remaining_fraction": self.remaining_fraction, "usage_state": self.usage_state, "reset_at": self.reset_at, "window": self.window, "evidence": self.evidence.to_record() if self.evidence else None}


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderHealth:
    provider: str
    state: str = "UNKNOWN"
    account: str | None = None
    installed: bool | None = None
    version: str | None = None
    authenticated: bool | None = None
    available: bool | None = None
    models: tuple[str, ...] = ()
    quota_pools: tuple[QuotaPool, ...] = ()
    reset_at: str | None = None
    last_checked: str | None = None
    error: str | None = None
    capabilities: tuple[str, ...] = ()
    evidence: HealthEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip() or self.state not in PROVIDER_HEALTH_STATES:
            raise ValueError("invalid provider health")
        for field_name in ("installed", "authenticated", "available"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{field_name} must be a boolean or null")
        for field_name in ("reset_at", "last_checked"):
            value = getattr(self, field_name)
            if value is not None:
                _health_time(value, field_name)
        object.__setattr__(self, "models", tuple(sorted({m.strip() for m in self.models if isinstance(m, str) and m.strip()})))
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))

    def to_record(self) -> dict[str, Any]:
        result = {"provider": self.provider, "state": self.state, "account": self.account, "installed": self.installed, "version": self.version, "authenticated": self.authenticated, "available": self.available, "models": list(self.models), "quota_pools": [p.to_record() for p in self.quota_pools], "reset_at": self.reset_at, "last_checked": self.last_checked, "error": self.error, "capabilities": list(self.capabilities), "evidence": self.evidence.to_record() if self.evidence else None}
        return result


class RuntimeErrorKind(str, Enum):
    CONFIGURATION = "configuration"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    MALFORMED_RESPONSE = "malformed_response"
    PROVIDER = "provider"
    UNKNOWN = "unknown"


class RuntimeAdapterError(ValueError):
    """A classified provider-adapter error; it is not Canonical State."""

    def __init__(self, kind: RuntimeErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class RuntimeAdapter(Protocol):
    """Minimal provider boundary; implementations must remain transport-optional."""

    provider: str
    execution_scope: str
    network_scope: str
    data_egress: str

    def prepare(self, request: RuntimeRequest) -> PreparedRequest:
        """Translate a neutral request into provider payload metadata."""
        ...

    def normalize(
        self,
        request: RuntimeRequest,
        payload: Mapping[str, Any],
    ) -> RuntimeResponse:
        """Normalize provider payload without deciding Canonical State."""
        ...


def is_runtime_adapter(value: object) -> bool:
    """Return whether an object satisfies the runtime adapter boundary."""

    return (
        all(callable(getattr(value, method, None)) for method in ("prepare", "normalize"))
        and all(
            isinstance(getattr(value, field, None), str)
            for field in ("provider", "execution_scope", "network_scope", "data_egress")
        )
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeRequest:
    """An immutable, provider-neutral inference request."""

    request_id: str
    model: str
    messages: tuple[Mapping[str, Any], ...]
    parameters: Mapping[str, Any] = field(default_factory=dict)
    response_schema: Mapping[str, Any] | None = None
    timeout_seconds: float = 60.0
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("RuntimeRequest에는 request_id가 필요합니다.")
        if not self.model.strip():
            raise ValueError("RuntimeRequest에는 model이 필요합니다.")
        if not self.messages:
            raise ValueError("RuntimeRequest에는 messages가 필요합니다.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds는 0보다 커야 합니다.")
        object.__setattr__(
            self,
            "messages",
            tuple(MappingProxyType(dict(message)) for message in self.messages),
        )
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        if self.response_schema is not None:
            if not isinstance(self.response_schema, Mapping):
                raise ValueError("response_schema는 JSON Schema 객체여야 합니다.")
            # Sampling parameters and output-shape enforcement are separate
            # contracts. Each provider adapter maps this schema to its own API.
            object.__setattr__(self, "response_schema", MappingProxyType(dict(self.response_schema)))


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedRequest:
    """A transport-neutral request prepared by an adapter."""

    request_id: str
    provider: str
    method: str
    url: str
    headers: Mapping[str, str]
    body: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.provider.strip() or not self.url.strip():
            raise ValueError("PreparedRequest의 request_id/provider/url이 필요합니다.")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeResponse:
    """Normalized provider output that still requires caller-side proposal policy."""

    request_id: str
    provider: str
    model: str
    text: str
    raw: Mapping[str, Any] = field(default_factory=dict)
    usage: Mapping[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    received_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.provider.strip() or not self.model.strip():
            raise ValueError("RuntimeResponse의 request_id/provider/model이 필요합니다.")
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


def response_was_truncated(response: RuntimeResponse) -> bool:
    """Say whether the provider stopped this response at the output limit.

    The only place that decides it, and it lives beside ``RuntimeResponse``
    because that is what it describes: every layer that receives one can ask
    without depending on the layer above it.

    The rule had been written four times before, in two shapes. Two callers
    compared against ``"length"`` alone and only from inside an ``except``, so
    a response that was cut off but still parsed was never reported as
    truncated -- and with a response schema the grammar closes the JSON for the
    model, which makes that the ordinary case rather than a rare one. Ollama
    spells the same condition ``"max_tokens"``, which those two never knew.

    Callers decide what it means for them; they do not decide what it is.
    """

    return str(response.finish_reason or "").strip().lower() in {"length", "max_tokens"}


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeObservation:
    """Trace metadata for one provider interaction, without model truth claims."""

    request_id: str
    provider: str
    endpoint: str
    model: str
    status: str
    started_at: str
    finished_at: str
    error_kind: RuntimeErrorKind | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "detail": self.detail,
        }
