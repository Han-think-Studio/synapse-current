"""Strict, explicit adapter for an externally supplied Infigraph JSON export.

The adapter owns no process or network client.  A caller must provide the
already-produced export, which is translated into Synapse's existing
``StructuralObservation`` contract and ``StructuralImportReceipt``.  The
external tool remains an evidence provider; it never becomes an authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.structural_adapter import (
    StructuralAdapterError,
    StructuralImport,
    StructuralImportReceipt,
)
from synapse.core.structural_observation import (
    STRUCTURAL_OBSERVATION_SCHEMA,
    StructuralObservationError,
    parse_structural_observation,
)


class InfigraphAdapterError(StructuralAdapterError):
    """Raised when a supplied Infigraph export violates the adapter contract."""


INFIGRAPH_EXPORT_SCHEMA = "infigraph.export.v1"
INFIGRAPH_PROVIDER = "infigraph"
INFIGRAPH_RELATION_ALIASES = MappingProxyType(
    {
        "CALL": "CALLS",
        "CALLS": "CALLS",
        "DEPENDENCY": "DEPENDS_ON",
        "DEPENDENCIES": "DEPENDS_ON",
        "DEPENDS_ON": "DEPENDS_ON",
        "IMPLEMENT": "IMPLEMENTS",
        "IMPLEMENTS": "IMPLEMENTS",
        "IMPORT": "IMPORTS",
        "IMPORTS": "IMPORTS",
        "INHERIT": "INHERITS",
        "INHERITS": "INHERITS",
        "READ": "READS",
        "READS": "READS",
        "ROUTE": "ROUTES_TO",
        "ROUTES_TO": "ROUTES_TO",
        "WRITE": "WRITES",
        "WRITES": "WRITES",
    }
)
_EXPORT_KEYS = frozenset(
    {
        "schema_version",
        "provider",
        "provider_version",
        "source_id",
        "workspace_hash",
        "captured_at",
        "scope",
        "nodes",
        "edges",
        "issues",
        "evidence",
        "unresolved",
    }
)
_MAX_EXPORT_BYTES = 8 * 1024 * 1024


def _text(value: Any, label: str, *, limit: int = 4_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InfigraphAdapterError(f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.")
    result = value.strip()
    if len(result) > limit:
        raise InfigraphAdapterError(f"{label}이(가) 너무 깁니다.")
    return result


def _strict_keys(value: Mapping[str, Any], allowed: set[str], required: set[str]) -> None:
    if any(not isinstance(key, str) for key in value):
        raise InfigraphAdapterError("Infigraph export의 객체 키는 문자열이어야 합니다.")
    unknown = set(value) - allowed
    if unknown:
        raise InfigraphAdapterError(f"알 수 없는 Infigraph export 필드입니다: {sorted(unknown)}")
    missing = required - set(value)
    if missing:
        raise InfigraphAdapterError(f"Infigraph export 필드가 없습니다: {sorted(missing)}")


def _records(value: Any, label: str) -> list[Mapping[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InfigraphAdapterError(f"{label}는 JSON 객체 배열이어야 합니다.")
    records: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InfigraphAdapterError(f"{label}[{index}]는 JSON 객체여야 합니다.")
        records.append(item)
    return records


def _relation_alias(value: Any) -> str:
    raw = _text(value, "edge.relation", limit=80)
    token = raw.replace("-", "_").replace(" ", "_").upper()
    try:
        return INFIGRAPH_RELATION_ALIASES[token]
    except KeyError as exc:
        raise InfigraphAdapterError(f"허용되지 않은 Infigraph relation입니다: {raw}") from exc


def _translate_edges(value: Any) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for index, item in enumerate(_records(value, "edges")):
        edge = dict(item)
        if "relation" not in edge:
            raise InfigraphAdapterError(f"edges[{index}]에 relation이 없습니다.")
        edge["relation"] = _relation_alias(edge["relation"])
        edges.append(edge)
    return edges


def _decode_export(value: Mapping[str, Any] | str) -> Mapping[str, Any]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_EXPORT_BYTES:
            raise InfigraphAdapterError("Infigraph export JSON이 너무 큽니다.")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InfigraphAdapterError("Infigraph export는 JSON 객체여야 합니다.") from exc
    if not isinstance(value, Mapping):
        raise InfigraphAdapterError("Infigraph export는 JSON 객체여야 합니다.")
    return value


def _observation_record(export: Mapping[str, Any]) -> dict[str, Any]:
    _strict_keys(
        export,
        set(_EXPORT_KEYS),
        set(_EXPORT_KEYS),
    )
    schema_version = _text(export["schema_version"], "export.schema_version", limit=120)
    if schema_version != INFIGRAPH_EXPORT_SCHEMA:
        raise InfigraphAdapterError(f"지원하지 않는 Infigraph export schema입니다: {schema_version}")
    provider = _text(export["provider"], "export.provider", limit=120).lower()
    if provider != INFIGRAPH_PROVIDER:
        raise InfigraphAdapterError(f"지원하지 않는 export provider입니다: {provider}")
    provider_version = _text(export["provider_version"], "export.provider_version", limit=120)
    nodes = _records(export["nodes"], "nodes")
    edges = _translate_edges(export["edges"])
    issues = _records(export["issues"], "issues")
    evidence = _records(export["evidence"], "evidence")
    scope = export["scope"]
    if not isinstance(scope, Mapping):
        raise InfigraphAdapterError("export.scope는 JSON 객체여야 합니다.")
    unresolved = export["unresolved"]
    if isinstance(unresolved, (str, bytes)) or not isinstance(unresolved, Sequence):
        raise InfigraphAdapterError("export.unresolved는 문자열 배열이어야 합니다.")
    return {
        "schema_version": STRUCTURAL_OBSERVATION_SCHEMA,
        "source_id": _text(export["source_id"], "export.source_id", limit=240),
        "sensor_type": INFIGRAPH_PROVIDER,
        "sensor_version": provider_version,
        "workspace_hash": _text(export["workspace_hash"], "export.workspace_hash", limit=240),
        "captured_at": _text(export["captured_at"], "export.captured_at", limit=120),
        "scope": dict(scope),
        "nodes": nodes,
        "edges": edges,
        "issues": issues,
        "evidence": evidence,
        "unresolved": list(unresolved),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class InfigraphJsonAdapter:
    """Explicit adapter for one already-produced Infigraph export."""

    adapter_id: str = "infigraph-json"
    adapter_version: str = "1"

    def __post_init__(self) -> None:
        if self.adapter_id != "infigraph-json":
            raise InfigraphAdapterError("adapter_id는 infigraph-json이어야 합니다.")
        object.__setattr__(self, "adapter_version", _text(self.adapter_version, "adapter_version", limit=120))

    def import_observation(self, value: Mapping[str, Any] | str) -> StructuralImport:
        try:
            export = _decode_export(value)
            observation = parse_structural_observation(_observation_record(export))
        except InfigraphAdapterError:
            raise
        except (StructuralObservationError, TypeError, ValueError) as exc:
            raise InfigraphAdapterError(f"Infigraph export를 observation으로 변환하지 못했습니다: {exc}") from exc
        receipt = StructuralImportReceipt(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            observation_id=observation.id,
            observation_hash=observation.observation_hash,
            source_id=observation.source_id,
            workspace_hash=observation.workspace_hash,
        )
        return StructuralImport(observation=observation, receipt=receipt)


__all__ = [
    "INFIGRAPH_EXPORT_SCHEMA",
    "INFIGRAPH_PROVIDER",
    "INFIGRAPH_RELATION_ALIASES",
    "InfigraphAdapterError",
    "InfigraphJsonAdapter",
]
