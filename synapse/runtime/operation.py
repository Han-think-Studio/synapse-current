"""Explicit, one-shot runtime operation receipts.

The provider adapters own request preparation and response normalization.  This
module adds only the shared execution boundary around an already selected
completer, so callers can see which step was reached without turning a runtime
response into Canonical State.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from synapse.runtime.contracts import (
    RuntimeAdapterError,
    RuntimeErrorKind,
    RuntimeObservation,
    RuntimeRequest,
    RuntimeResponse,
    utc_now,
)

RUNTIME_OPERATION_SCHEMA = "runtime.operation.v1"
RUNTIME_OPERATION_STAGES = (
    "REQUEST_VALIDATED",
    "DISPATCH_APPROVED",
    "PROVIDER_INVOKED",
    "RESPONSE_NORMALIZED",
)
RUNTIME_OPERATION_STATUSES = ("SUCCEEDED", "FAILED")
MAX_RUNTIME_ERROR_DETAIL = 2000
RuntimeCompleter = Callable[[RuntimeRequest], RuntimeResponse]


class RuntimeOperationError(ValueError):
    """Raised when an explicit runtime operation cannot be started or consumed."""


def _json_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text_digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _response_metadata(response: RuntimeResponse) -> dict[str, Any]:
    return {
        "request_id": response.request_id,
        "provider": response.provider,
        "model": response.model,
        "text_sha256": _text_digest(response.text),
        "text_length": len(response.text),
        "usage": dict(response.usage),
        "finish_reason": response.finish_reason,
    }


def _bounded_detail(detail: object) -> str:
    return str(detail)[:MAX_RUNTIME_ERROR_DETAIL]


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeOperationResult:
    """Immutable, non-authoritative receipt for one explicit runtime call."""

    request: RuntimeRequest
    provider: str
    response: RuntimeResponse | None
    observation: RuntimeObservation
    stages: tuple[str, ...]
    status: str
    endpoint: str
    response_hash: str | None = None
    schema_version: str = RUNTIME_OPERATION_SCHEMA
    id: str = ""
    automatic: bool = False
    retry_count: int = 0
    fallback_used: bool = False
    filesystem_mutation: bool = False
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_OPERATION_SCHEMA:
            raise RuntimeOperationError("지원하지 않는 runtime operation schema입니다.")
        if not isinstance(self.request, RuntimeRequest):
            raise RuntimeOperationError("request는 RuntimeRequest이어야 합니다.")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise RuntimeOperationError("provider가 필요합니다.")
        provider = self.provider.strip().lower()
        object.__setattr__(self, "provider", provider)
        if not isinstance(self.response, RuntimeResponse | type(None)):
            raise RuntimeOperationError("response 타입이 잘못되었습니다.")
        if not isinstance(self.observation, RuntimeObservation):
            raise RuntimeOperationError("observation은 RuntimeObservation이어야 합니다.")
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise RuntimeOperationError("endpoint가 필요합니다.")
        if self.status not in RUNTIME_OPERATION_STATUSES:
            raise RuntimeOperationError("지원하지 않는 runtime operation 상태입니다.")
        stages = tuple(self.stages)
        if self.status == "SUCCEEDED":
            if stages != RUNTIME_OPERATION_STAGES:
                raise RuntimeOperationError("성공 operation 단계 순서가 계약과 다릅니다.")
            if self.response is None:
                raise RuntimeOperationError("성공 operation에는 response가 필요합니다.")
        elif stages != RUNTIME_OPERATION_STAGES[:-1]:
            raise RuntimeOperationError("실패 operation 단계 순서가 계약과 다릅니다.")
        object.__setattr__(self, "stages", stages)

        if self.observation.request_id != self.request.request_id:
            raise RuntimeOperationError("runtime observation request identity가 다릅니다.")
        if self.observation.provider.strip().lower() != provider:
            raise RuntimeOperationError("runtime observation provider identity가 다릅니다.")
        if self.observation.endpoint != self.endpoint:
            raise RuntimeOperationError("runtime observation endpoint가 다릅니다.")
        if self.observation.status != self.status:
            raise RuntimeOperationError("runtime observation status가 operation과 다릅니다.")
        if self.status == "SUCCEEDED":
            assert self.response is not None
            if self.response.request_id != self.request.request_id:
                raise RuntimeOperationError("normalized response request identity가 다릅니다.")
            if self.response.provider.strip().lower() != provider:
                raise RuntimeOperationError("normalized response provider identity가 다릅니다.")
            expected_response_hash = _json_digest(_response_metadata(self.response))
            if self.response_hash is not None and self.response_hash != expected_response_hash:
                raise RuntimeOperationError("response metadata hash가 다릅니다.")
            object.__setattr__(self, "response_hash", expected_response_hash)
            if self.observation.error_kind is not None:
                raise RuntimeOperationError("성공 operation에는 error_kind가 없어야 합니다.")
        elif self.response is not None:
            raise RuntimeOperationError("실패 operation에는 normalized response가 없어야 합니다.")
        elif self.observation.error_kind is None:
            raise RuntimeOperationError("실패 operation에는 error_kind가 필요합니다.")
        for value, label in (
            (self.automatic, "operation.automatic"),
            (self.fallback_used, "operation.fallback_used"),
            (self.filesystem_mutation, "operation.filesystem_mutation"),
            (self.canonical_mutation, "operation.canonical_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise RuntimeOperationError(f"{label}은(는) false여야 합니다.")
        if not isinstance(self.retry_count, int) or self.retry_count != 0:
            raise RuntimeOperationError("retry_count는 0이어야 합니다.")

        operation_payload = {
            "schema_version": self.schema_version,
            "request_id": self.request.request_id,
            "provider": provider,
            "model": self.request.model,
            "status": self.status,
            "endpoint": self.endpoint,
            "stages": list(stages),
            "response_hash": self.response_hash,
        }
        expected_id = f"runtime-operation:{_json_digest(operation_payload).removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise RuntimeOperationError("runtime operation id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"

    @property
    def failed(self) -> bool:
        return self.status == "FAILED"

    def require_response(self) -> RuntimeResponse:
        if self.response is None or not self.succeeded:
            detail = self.observation.detail or "runtime operation이 실패했습니다."
            raise RuntimeOperationError(detail)
        return self.response

    def to_record(self) -> dict[str, Any]:
        response_record: dict[str, Any] | None = None
        if self.response is not None:
            metadata = _response_metadata(self.response)
            response_record = {
                "request_id": metadata["request_id"],
                "provider": metadata["provider"],
                "model": metadata["model"],
                "text_sha256": metadata["text_sha256"],
                "text_length": metadata["text_length"],
                "usage": metadata["usage"],
                "finish_reason": metadata["finish_reason"],
            }
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "request_id": self.request.request_id,
            "provider": self.provider,
            "requested_model": self.request.model,
            "endpoint": self.endpoint,
            "status": self.status,
            "stages": list(self.stages),
            "observation": self.observation.to_dict(),
            "response_hash": self.response_hash,
            "response_metadata": response_record,
            "automatic": False,
            "retry_count": 0,
            "fallback_used": False,
            "filesystem_mutation": False,
            "canonical_mutation": False,
        }


def run_explicit_runtime_operation(
    request: RuntimeRequest,
    *,
    provider: str,
    complete: RuntimeCompleter,
    dispatch_approved: bool,
    endpoint: str | None = None,
    clock: Callable[[], str] = utc_now,
) -> RuntimeOperationResult:
    """Perform exactly one explicitly approved completion and return its receipt."""

    if not isinstance(request, RuntimeRequest):
        raise RuntimeOperationError("request는 RuntimeRequest이어야 합니다.")
    if not isinstance(provider, str) or not provider.strip():
        raise RuntimeOperationError("provider가 필요합니다.")
    normalized_provider = provider.strip().lower()
    if dispatch_approved is not True:
        raise RuntimeOperationError("runtime operation에는 dispatch_approved=true가 필요합니다.")
    if not callable(complete):
        raise RuntimeOperationError("complete는 호출 가능한 provider completer이어야 합니다.")
    normalized_endpoint = str(endpoint or f"provider://{normalized_provider}").strip()
    if not normalized_endpoint:
        raise RuntimeOperationError("endpoint가 필요합니다.")
    if not callable(clock):
        raise RuntimeOperationError("clock은 호출 가능해야 합니다.")

    started_at = clock()
    completed_stages = RUNTIME_OPERATION_STAGES[:-1]
    try:
        response = complete(request)
        if not isinstance(response, RuntimeResponse):
            raise RuntimeOperationError("provider가 RuntimeResponse를 반환하지 않았습니다.")
        if response.request_id != request.request_id:
            raise RuntimeOperationError("provider response request identity가 요청과 다릅니다.")
        if response.provider.strip().lower() != normalized_provider:
            raise RuntimeOperationError("provider response identity가 선택 provider와 다릅니다.")
        finished_at = clock()
        observation = RuntimeObservation(
            request_id=request.request_id,
            provider=normalized_provider,
            endpoint=normalized_endpoint,
            model=response.model,
            status="SUCCEEDED",
            started_at=started_at,
            finished_at=finished_at,
        )
        return RuntimeOperationResult(
            request=request,
            provider=normalized_provider,
            response=response,
            observation=observation,
            stages=RUNTIME_OPERATION_STAGES,
            status="SUCCEEDED",
            endpoint=normalized_endpoint,
        )
    except RuntimeOperationError as exc:
        error_kind = RuntimeErrorKind.MALFORMED_RESPONSE
        detail = str(exc)
    except RuntimeAdapterError as exc:
        error_kind = exc.kind
        detail = str(exc)
    except ValueError as exc:
        error_kind = RuntimeErrorKind.PROVIDER
        detail = str(exc)
    except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive provider boundary
        error_kind = RuntimeErrorKind.UNKNOWN
        detail = str(exc)
    except (AttributeError, IndexError, KeyError, TypeError) as exc:  # pragma: no cover - defensive provider boundary
        error_kind = RuntimeErrorKind.UNKNOWN
        detail = str(exc)

    finished_at = clock()
    observation = RuntimeObservation(
        request_id=request.request_id,
        provider=normalized_provider,
        endpoint=normalized_endpoint,
        model=request.model,
        status="FAILED",
        started_at=started_at,
        finished_at=finished_at,
        error_kind=error_kind,
        detail=_bounded_detail(detail),
    )
    return RuntimeOperationResult(
        request=request,
        provider=normalized_provider,
        response=None,
        observation=observation,
        stages=completed_stages,
        status="FAILED",
        endpoint=normalized_endpoint,
    )


__all__ = [
    "MAX_RUNTIME_ERROR_DETAIL",
    "RUNTIME_OPERATION_SCHEMA",
    "RUNTIME_OPERATION_STAGES",
    "RUNTIME_OPERATION_STATUSES",
    "RuntimeCompleter",
    "RuntimeOperationError",
    "RuntimeOperationResult",
    "run_explicit_runtime_operation",
]
