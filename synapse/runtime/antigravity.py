"""RuntimeAdapter and transport boundary for the user-context Antigravity broker."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from synapse.runtime.antigravity_broker import (
    ALLOWED_OPERATIONS,
    BrokerProtocolError,
    invoke_named_pipe,
)
from synapse.runtime.contracts import (
    PreparedRequest,
    RuntimeAdapterError,
    RuntimeErrorKind,
    RuntimeRequest,
    RuntimeResponse,
)
from synapse.runtime.policy import validate_endpoint_policy


def _pipe_name(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "pipe" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "Antigravity broker endpoint는 pipe:// 이름이어야 합니다.")
    return parsed.netloc


class AntigravityAdapter:
    """Prepare broker invoke requests without probing or invoking ``agy``."""

    provider = "antigravity"
    execution_scope = "service"
    network_scope = "none"
    data_egress = "external_allowed"

    def __init__(self, endpoint: str = "pipe://synapse-antigravity") -> None:
        normalized = endpoint.strip()
        _pipe_name(normalized)
        validate_endpoint_policy(
            normalized,
            network_scope=self.network_scope,
            data_egress=self.data_egress,
        )
        self.endpoint = normalized

    def prepare(self, request: RuntimeRequest) -> PreparedRequest:
        prompt = "\n".join(str(message.get("content", "")) for message in request.messages)
        return PreparedRequest(
            request_id=request.request_id,
            provider=self.provider,
            method="BROKER",
            url=self.endpoint,
            headers={},
            body={
                "operation": "antigravity.invoke",
                "task_id": request.request_id,
                "prompt": prompt,
                "model": request.model,
                "timeout": request.timeout_seconds,
                "output_format": "json",
            },
        )

    def normalize(self, request: RuntimeRequest, payload: Mapping[str, Any]) -> RuntimeResponse:
        if not isinstance(payload, Mapping):
            raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, "broker 응답이 JSON object가 아닙니다.")
        status = payload.get("status")
        if status != "SUCCEEDED":
            state = payload.get("provider_state")
            kind = {
                "AUTH_REQUIRED": RuntimeErrorKind.AUTHENTICATION,
                "AUTH_CONTEXT_BLOCKED": RuntimeErrorKind.AUTHENTICATION,
                "COOLDOWN": RuntimeErrorKind.TIMEOUT,
                "NOT_INSTALLED": RuntimeErrorKind.CONFIGURATION,
            }.get(state, RuntimeErrorKind.PROVIDER)
            detail = payload.get("sanitized_error") or f"Antigravity broker status: {status}"
            raise RuntimeAdapterError(kind, str(detail)[:4000])
        text = payload.get("sanitized_result", payload.get("sanitized_stdout"))
        if not isinstance(text, str):
            raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, "broker 결과에 sanitized_result가 없습니다.")
        # "stop" is a claim about how the run ended, and the broker only lets us
        # make it for a run its own cap did not cut. Reporting a trimmed result
        # as a clean stop defeats every truncation check downstream, which asks
        # the response what happened and has no other way to find out.
        truncated = bool(payload.get("result_truncated"))
        return RuntimeResponse(
            request_id=request.request_id,
            provider=self.provider,
            model=request.model,
            text=text,
            raw=payload,
            usage={"quota_state": payload.get("quota_state", "UNKNOWN")},
            finish_reason="length" if truncated else "stop",
        )


BrokerInvoke = Callable[[str, Mapping[str, Any], bytes, float], Mapping[str, Any]]


class AntigravityBrokerTransport:
    """Send one prepared structured request through the existing broker pipe."""

    def __init__(
        self,
        *,
        pipe_name: str = r"\\.\pipe\synapse-antigravity",
        authkey: bytes,
        invoke: BrokerInvoke | None = None,
    ) -> None:
        if not isinstance(authkey, bytes) or not authkey:
            raise ValueError("broker authkey must be a non-empty ephemeral bytes value")
        self.pipe_name = pipe_name
        self.authkey = authkey
        self._invoke = invoke or self._invoke_default

    def _invoke_default(self, pipe_name: str, body: Mapping[str, Any], authkey: bytes, timeout: float) -> Mapping[str, Any]:
        return invoke_named_pipe(pipe_name, body, authkey=authkey, timeout=timeout)

    def send(self, request: PreparedRequest, *, timeout_seconds: float) -> Mapping[str, Any]:
        if request.provider != "antigravity" or request.method != "BROKER":
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "AntigravityBrokerTransport에 맞지 않는 request입니다.")
        if request.url != "pipe://synapse-antigravity":
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "허용되지 않은 Antigravity broker endpoint입니다.")
        if request.body.get("operation") not in ALLOWED_OPERATIONS:
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "허용되지 않은 broker operation입니다.")
        try:
            payload = self._invoke(self.pipe_name, request.body, self.authkey, timeout_seconds)
        except TimeoutError as exc:
            raise RuntimeAdapterError(RuntimeErrorKind.TIMEOUT, "Antigravity broker 응답 시간 초과") from exc
        except (OSError, BrokerProtocolError) as exc:
            raise RuntimeAdapterError(RuntimeErrorKind.TRANSPORT, "Antigravity broker 전송 실패") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, "broker가 JSON object를 반환하지 않았습니다.")
        return payload


__all__ = ["AntigravityAdapter", "AntigravityBrokerTransport"]
