"""Adapter for a separately installed modern Ollama service.

Synapse keeps only this provider-neutral connection boundary; it does not
bundle a portable Ollama/IPEX runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

from synapse.runtime.contracts import (
    PreparedRequest,
    RuntimeAdapterError,
    RuntimeErrorKind,
    RuntimeRequest,
    RuntimeResponse,
)
from synapse.runtime.http_transport import JsonTransport, UrllibJsonTransport
from synapse.runtime.policy import validate_endpoint_policy


class OllamaAdapter:
    """Prepare modern Ollama requests without implicit network I/O."""

    provider = "ollama"
    execution_scope = "service"
    network_scope = "loopback"
    data_egress = "local_only"

    def __init__(self, base_url: str = "http://127.0.0.1:11434") -> None:
        normalized = base_url.strip()
        if not normalized:
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "Ollama base_url이 비어 있습니다.")
        validate_endpoint_policy(
            normalized,
            network_scope=self.network_scope,
            data_egress=self.data_egress,
        )
        self.base_url = normalized.rstrip("/") + "/"

    def prepare(self, request: RuntimeRequest) -> PreparedRequest:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [dict(message) for message in request.messages],
            "stream": False,
        }
        if request.parameters:
            body["options"] = dict(request.parameters)
        if request.response_schema is not None:
            # Ollama's structured-output field is top-level "format", not nested
            # under "options" -- unlike lmstudio.py's response_format, this has
            # NOT been verified against a live Ollama instance (none was running
            # during the 2026-09-03 design session). Treat as unverified until a
            # live test confirms it.
            body["format"] = dict(request.response_schema)
        return PreparedRequest(
            request_id=request.request_id,
            provider=self.provider,
            method="POST",
            url=urljoin(self.base_url, "api/chat"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=body,
        )

    def normalize(self, request: RuntimeRequest, payload: Mapping[str, Any]) -> RuntimeResponse:
        message = payload.get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
            raise RuntimeAdapterError(
                RuntimeErrorKind.MALFORMED_RESPONSE,
                "Ollama 응답에 message.content가 없습니다.",
            )
        usage = {
            key: payload[key]
            for key in ("prompt_eval_count", "eval_count", "total_duration")
            if key in payload
        }
        return RuntimeResponse(
            request_id=request.request_id,
            provider=self.provider,
            model=str(payload.get("model") or request.model),
            text=message["content"],
            raw=payload,
            usage=usage,
            finish_reason=str(payload.get("done_reason")) if payload.get("done_reason") else None,
        )

    def complete(
        self,
        request: RuntimeRequest,
        *,
        transport: JsonTransport | None = None,
    ) -> RuntimeResponse:
        prepared = self.prepare(request)
        payload = (transport or UrllibJsonTransport()).send(
            prepared,
            timeout_seconds=request.timeout_seconds,
        )
        return self.normalize(request, payload)


__all__ = ["OllamaAdapter"]
