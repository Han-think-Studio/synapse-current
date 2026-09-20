"""Explicit opt-in JSON HTTP transport for provider adapters.

Importing this module does not contact a server.  A caller must explicitly
invoke ``UrllibJsonTransport.send``; failures are classified and never become
Canonical State automatically.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

from synapse.runtime.contracts import (
    PreparedRequest,
    RuntimeAdapterError,
    RuntimeErrorKind,
)


def _http_error_detail(exc: urllib.error.HTTPError, *, limit: int = 512) -> str:
    """Return a bounded provider error detail without exposing a full body."""

    try:
        raw = exc.read(4096).decode("utf-8", errors="replace").strip()
    except OSError:
        raw = ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        detail = raw
    else:
        detail_value: Any = payload
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                detail_value = error.get("message") or error.get("detail") or error
            elif error:
                detail_value = error
            elif payload.get("message"):
                detail_value = payload["message"]
        detail = str(detail_value)
    return detail[:limit]


class JsonTransport(Protocol):
    def send(self, request: PreparedRequest, *, timeout_seconds: float) -> Mapping[str, Any]:
        """Send one prepared request and return a JSON object."""
        ...


class UrllibJsonTransport:
    """Small stdlib transport used only when a caller explicitly opts in."""

    def send(self, request: PreparedRequest, *, timeout_seconds: float) -> Mapping[str, Any]:
        body = json.dumps(dict(request.body), ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            request.url,
            data=body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urllib.request.urlopen(http_request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            kind = {
                401: RuntimeErrorKind.AUTHENTICATION,
                403: RuntimeErrorKind.AUTHENTICATION,
                408: RuntimeErrorKind.TIMEOUT,
                429: RuntimeErrorKind.RATE_LIMIT,
                504: RuntimeErrorKind.TIMEOUT,
            }.get(exc.code, RuntimeErrorKind.PROVIDER)
            detail = _http_error_detail(exc)
            suffix = f": {detail}" if detail else ""
            raise RuntimeAdapterError(kind, f"{request.provider} HTTP {exc.code}{suffix}") from exc
        except TimeoutError as exc:
            raise RuntimeAdapterError(RuntimeErrorKind.TIMEOUT, f"{request.provider} 요청 시간 초과") from exc
        except urllib.error.URLError as exc:
            kind = RuntimeErrorKind.TIMEOUT if isinstance(exc.reason, TimeoutError) else RuntimeErrorKind.TRANSPORT
            label = "요청 시간 초과" if kind is RuntimeErrorKind.TIMEOUT else f"전송 실패: {exc.reason}"
            raise RuntimeAdapterError(kind, f"{request.provider} {label}") from exc
        except OSError as exc:
            raise RuntimeAdapterError(RuntimeErrorKind.TRANSPORT, f"{request.provider} 전송 실패") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeAdapterError(
                RuntimeErrorKind.MALFORMED_RESPONSE,
                f"{request.provider} 응답이 JSON이 아닙니다.",
            ) from exc
        if not isinstance(payload, Mapping):
            raise RuntimeAdapterError(
                RuntimeErrorKind.MALFORMED_RESPONSE,
                f"{request.provider} 응답이 JSON object가 아닙니다.",
            )
        return payload
