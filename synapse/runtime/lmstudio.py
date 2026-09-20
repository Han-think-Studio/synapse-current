"""Dry-run and response normalization adapter for LM Studio's local API."""

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


class LMStudioAdapter:
    """Prepare OpenAI-compatible LM Studio requests without network I/O."""

    provider = "lmstudio"
    execution_scope = "service"
    network_scope = "loopback"
    data_egress = "local_only"

    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1") -> None:
        normalized = base_url.strip()
        if not normalized:
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "LM Studio base_url이 비어 있습니다.")
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
        }
        body.update(dict(request.parameters))
        if request.response_schema is not None:
            # LM Studio's OpenAI-compatible server only accepts "json_schema" or
            # "text" for response_format.type -- verified live 2026-09-03
            # (a bare {"type": "json_object"} request is rejected with HTTP 400).
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "synapse_structured_output",
                    "strict": True,
                    "schema": dict(request.response_schema),
                },
            }
        return PreparedRequest(
            request_id=request.request_id,
            provider=self.provider,
            method="POST",
            url=urljoin(self.base_url, "chat/completions"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=body,
        )

    def normalize(self, request: RuntimeRequest, payload: Mapping[str, Any]) -> RuntimeResponse:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise RuntimeAdapterError(
                RuntimeErrorKind.MALFORMED_RESPONSE,
                "LM Studio 응답에 choices[0]가 없습니다.",
            )
        message = choices[0].get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
            raise RuntimeAdapterError(
                RuntimeErrorKind.MALFORMED_RESPONSE,
                "LM Studio 응답의 choices[0].message.content가 문자열이 아닙니다.",
            )
        usage = payload.get("usage", {})
        if not isinstance(usage, Mapping):
            usage = {}
        finish_reason = choices[0].get("finish_reason")
        return RuntimeResponse(
            request_id=request.request_id,
            provider=self.provider,
            model=str(payload.get("model") or request.model),
            text=message["content"],
            raw=payload,
            usage=usage,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
        )

    def normalize_creative(self, *, stage: str, response: RuntimeResponse | Mapping[str, Any] | str,
                           seed_hash: str, global_map: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Pass an existing LM Studio result through the creative boundary."""
        from synapse.core.creative_runtime_bridge import normalize_adapter_creative_response
        return normalize_adapter_creative_response(stage=stage, response=response,
                                                   seed_hash=seed_hash, global_map=global_map)

    def complete(
        self,
        request: RuntimeRequest,
        *,
        transport: JsonTransport | None = None,
    ) -> RuntimeResponse:
        """Explicitly send one request; never called during adapter construction."""

        prepared = self.prepare(request)
        payload = (transport or UrllibJsonTransport()).send(
            prepared,
            timeout_seconds=request.timeout_seconds,
        )
        return self.normalize(request, payload)
