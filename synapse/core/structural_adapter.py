"""Provider-neutral intake for immutable structural observations.

The adapter boundary translates one external representation into the existing
``StructuralObservation`` evidence contract.  It deliberately does not scan a
workspace, call a service, persist an observation, or decide that an observed
relationship is Canonical truth.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_observation import (
    STRUCTURAL_OBSERVATION_SCHEMA,
    StructuralObservation,
    StructuralObservationError,
    parse_structural_observation,
)


class StructuralAdapterError(ValueError):
    """Raised when an adapter boundary cannot produce safe evidence."""


STRUCTURAL_ADAPTER_SCHEMA = "structural.adapter.v1"
_ADAPTER_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _text(value: Any, label: str, *, limit: int = 240) -> str:
    if not isinstance(value, str):
        raise StructuralAdapterError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if not result:
        raise StructuralAdapterError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise StructuralAdapterError(f"{label}이(가) 너무 깁니다.")
    return result


def _adapter_token(value: Any, label: str) -> str:
    result = _text(value, label, limit=120).lower()
    if _ADAPTER_TOKEN.fullmatch(result) is None:
        raise StructuralAdapterError(
            f"{label}은(는) 소문자 영숫자·점·밑줄·하이픈만 사용할 수 있습니다."
        )
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralImportReceipt:
    """Deterministic proof of one adapter-to-observation translation."""

    adapter_id: str
    adapter_version: str
    observation_id: str
    observation_hash: str
    source_id: str
    workspace_hash: str
    schema_version: str = STRUCTURAL_ADAPTER_SCHEMA
    id: str = ""
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        schema_version = _text(self.schema_version, "receipt.schema_version", limit=120)
        if schema_version != STRUCTURAL_ADAPTER_SCHEMA:
            raise StructuralAdapterError(
                f"지원하지 않는 structural adapter schema입니다: {schema_version}"
            )
        adapter_id = _adapter_token(self.adapter_id, "receipt.adapter_id")
        adapter_version = _text(self.adapter_version, "receipt.adapter_version", limit=120)
        observation_id = _text(self.observation_id, "receipt.observation_id", limit=500)
        observation_hash = _text(self.observation_hash, "receipt.observation_hash", limit=240)
        source_id = _text(self.source_id, "receipt.source_id", limit=240)
        workspace_hash = _text(self.workspace_hash, "receipt.workspace_hash", limit=240)
        if not isinstance(self.canonical_mutation, bool):
            raise StructuralAdapterError("receipt.canonical_mutation은 boolean이어야 합니다.")
        if not isinstance(self.filesystem_mutation, bool):
            raise StructuralAdapterError("receipt.filesystem_mutation은 boolean이어야 합니다.")
        if self.canonical_mutation or self.filesystem_mutation:
            raise StructuralAdapterError("structural import receipt는 상태나 파일을 변경할 수 없습니다.")

        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "adapter_version", adapter_version)
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "observation_hash", observation_hash)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "workspace_hash", workspace_hash)

        receipt_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-import:{receipt_hash.removeprefix('sha256:')}"
        receipt_id = _text(self.id, "receipt.id", limit=500) if self.id else ""
        if receipt_id and receipt_id != expected_id:
            raise StructuralAdapterError("structural import receipt id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "observation_id": self.observation_id,
            "observation_hash": self.observation_hash,
            "source_id": self.source_id,
            "workspace_hash": self.workspace_hash,
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "observation_id": self.observation_id,
            "observation_hash": self.observation_hash,
            "source_id": self.source_id,
            "workspace_hash": self.workspace_hash,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralImport:
    """An immutable observation and the receipt for the adapter boundary."""

    observation: StructuralObservation
    receipt: StructuralImportReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.observation, StructuralObservation):
            raise StructuralAdapterError("import.observation 타입이 잘못되었습니다.")
        if not isinstance(self.receipt, StructuralImportReceipt):
            raise StructuralAdapterError("import.receipt 타입이 잘못되었습니다.")
        if self.receipt.observation_id != self.observation.id:
            raise StructuralAdapterError("receipt observation_id가 observation과 다릅니다.")
        if self.receipt.observation_hash != self.observation.observation_hash:
            raise StructuralAdapterError("receipt observation_hash가 observation과 다릅니다.")
        if self.receipt.source_id != self.observation.source_id:
            raise StructuralAdapterError("receipt source_id가 observation과 다릅니다.")
        if self.receipt.workspace_hash != self.observation.workspace_hash:
            raise StructuralAdapterError("receipt workspace_hash가 observation과 다릅니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "observation": self.observation.to_record(),
            "receipt": self.receipt.to_record(),
        }


@runtime_checkable
class StructuralObservationAdapter(Protocol):
    """Minimal boundary implemented by local or external observation adapters."""

    adapter_id: str
    adapter_version: str

    def import_observation(
        self, value: Mapping[str, Any] | str
    ) -> StructuralImport:
        """Translate one representation into an immutable structural import."""


@dataclass(frozen=True, slots=True, kw_only=True)
class JsonStructuralObservationAdapter:
    """The first local adapter for the canonical strict observation JSON shape."""

    adapter_id: str = "json"
    adapter_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _adapter_token(self.adapter_id, "adapter_id"))
        object.__setattr__(self, "adapter_version", _text(self.adapter_version, "adapter_version", limit=120))

    def import_observation(self, value: Mapping[str, Any] | str) -> StructuralImport:
        try:
            observation = parse_structural_observation(value)
        except (StructuralObservationError, TypeError) as exc:
            raise StructuralAdapterError(f"JSON structural observation을 가져오지 못했습니다: {exc}") from exc

        if observation.schema_version != STRUCTURAL_OBSERVATION_SCHEMA:
            raise StructuralAdapterError("지원하지 않는 StructuralObservation schema입니다.")
        receipt = StructuralImportReceipt(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            observation_id=observation.id,
            observation_hash=observation.observation_hash,
            source_id=observation.source_id,
            workspace_hash=observation.workspace_hash,
        )
        return StructuralImport(observation=observation, receipt=receipt)


def import_structural_observation(
    value: Mapping[str, Any] | str,
    *,
    adapter: StructuralObservationAdapter | None = None,
) -> StructuralImport:
    """Import one observation through an explicit adapter boundary."""

    selected = adapter or JsonStructuralObservationAdapter()
    if not isinstance(selected, StructuralObservationAdapter):
        raise StructuralAdapterError("유효한 StructuralObservationAdapter가 필요합니다.")
    try:
        imported = selected.import_observation(value)
    except StructuralAdapterError:
        raise
    except (TypeError, ValueError) as exc:
        raise StructuralAdapterError(f"structural adapter import가 실패했습니다: {exc}") from exc
    if not isinstance(imported, StructuralImport):
        raise StructuralAdapterError("adapter는 StructuralImport를 반환해야 합니다.")
    return imported


__all__ = [
    "STRUCTURAL_ADAPTER_SCHEMA",
    "JsonStructuralObservationAdapter",
    "StructuralAdapterError",
    "StructuralImport",
    "StructuralImportReceipt",
    "StructuralObservationAdapter",
    "import_structural_observation",
]
