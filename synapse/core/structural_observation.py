"""Provider-neutral structural observations for the Phase 29 P1 contract.

Structural sensors report what they observed; they do not assert Canonical
State.  This module therefore contains only immutable, strictly validated
evidence records.  It deliberately has no filesystem, network, model, or
database dependency.  A later adapter may translate a sensor's JSON export
into these records, while a later verification path decides what (if
anything) should become a proposal.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.idea_session import canonical_hash, utc_now


class StructuralObservationError(ValueError):
    """Raised when a structural observation violates the P1 contract."""


STRUCTURAL_OBSERVATION_SCHEMA = "structural.observation.v1"
STRUCTURAL_RELATIONS = frozenset(
    {
        "CALLS",
        "DEPENDS_ON",
        "IMPLEMENTS",
        "INHERITS",
        "IMPORTS",
        "READS",
        "ROUTES_TO",
        "WRITES",
    }
)
STRUCTURAL_ISSUE_SEVERITIES = frozenset({"INFO", "WARNING", "ERROR"})
_MAX_NODES = 20_000
_MAX_EDGES = 50_000
_MAX_ISSUES = 10_000
_MAX_EVIDENCE = 50_000
_MAX_UNRESOLVED = 20_000
_MAX_TEXT = 4_000
_MAX_ATTRIBUTE_DEPTH = 12


def _text(value: Any, label: str, *, limit: int = _MAX_TEXT, required: bool = True) -> str:
    if not isinstance(value, str):
        raise StructuralObservationError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise StructuralObservationError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise StructuralObservationError(f"{label}이(가) 너무 깁니다.")
    return result


def _locator(value: Any, label: str) -> str:
    return _text(value, label, required=False).replace("\\", "/")


def _strings(
    value: Sequence[str],
    label: str,
    *,
    max_items: int = _MAX_UNRESOLVED,
    item_limit: int = 1_000,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructuralObservationError(f"{label}는 문자열 배열이어야 합니다.")
    if len(value) > max_items:
        raise StructuralObservationError(f"{label} 항목이 너무 많습니다.")
    result = []
    for item in value:
        result.append(_text(item, label, limit=item_limit))
    return tuple(sorted(set(result)))


def _freeze_json(value: Any, label: str, *, depth: int = 0) -> Any:
    """Deep-freeze JSON-shaped data so frozen records are actually immutable."""

    if depth > _MAX_ATTRIBUTE_DEPTH:
        raise StructuralObservationError(f"{label} 중첩 깊이가 너무 깊습니다.")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructuralObservationError(f"{label}에 유한하지 않은 숫자가 있습니다.")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise StructuralObservationError(f"{label}의 객체 키는 비어 있지 않은 문자열이어야 합니다.")
            frozen[key] = _freeze_json(item, f"{label}.{key}", depth=depth + 1)
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, label, depth=depth + 1) for item in value)
    raise StructuralObservationError(f"{label}에는 JSON 값만 허용됩니다.")


def _freeze_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructuralObservationError(f"{label}는 JSON 객체여야 합니다.")
    frozen = _freeze_json(value, label)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded by _freeze_json
        raise StructuralObservationError(f"{label}는 JSON 객체여야 합니다.")
    return frozen


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _strict_keys(value: Mapping[str, Any], allowed: set[str], label: str, required: set[str] | None = None) -> None:
    if any(not isinstance(key, str) for key in value):
        raise StructuralObservationError(f"{label}의 객체 키는 문자열이어야 합니다.")
    unknown = set(value) - allowed
    if unknown:
        raise StructuralObservationError(f"{label}에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    missing = (required or set()) - set(value)
    if missing:
        raise StructuralObservationError(f"{label}에 필수 필드가 없습니다: {sorted(missing)}")


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralEvidence:
    """A source-located piece of evidence attached to an observation."""

    id: str
    source_id: str
    kind: str = "sensor_output"
    locator: str = ""
    digest: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "evidence.id", limit=240))
        object.__setattr__(self, "source_id", _text(self.source_id, "evidence.source_id", limit=240))
        object.__setattr__(self, "kind", _text(self.kind, "evidence.kind", limit=120))
        object.__setattr__(self, "locator", _locator(self.locator, "evidence.locator"))
        if self.digest is not None:
            object.__setattr__(self, "digest", _text(self.digest, "evidence.digest", limit=240))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "evidence.metadata"))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "kind": self.kind,
            "locator": self.locator,
            "digest": self.digest,
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralNode:
    """One observed symbol/component/file node."""

    id: str
    kind: str
    label: str
    locator: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "node.id", limit=320))
        object.__setattr__(self, "kind", _text(self.kind, "node.kind", limit=120))
        object.__setattr__(self, "label", _text(self.label, "node.label"))
        object.__setattr__(self, "locator", _locator(self.locator, "node.locator"))
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes, "node.attributes"))
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs, "node.evidence_refs", item_limit=240))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "locator": self.locator,
            "attributes": _thaw_json(self.attributes),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True, order=True, kw_only=True)
class StructuralEdge:
    """One observed directed structural relationship."""

    source_id: str
    target_id: str
    relation: str
    evidence_refs: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _text(self.source_id, "edge.source_id", limit=320))
        object.__setattr__(self, "target_id", _text(self.target_id, "edge.target_id", limit=320))
        relation = _text(self.relation, "edge.relation", limit=80).upper()
        if relation not in STRUCTURAL_RELATIONS:
            raise StructuralObservationError(f"지원하지 않는 structural relation입니다: {relation}")
        object.__setattr__(self, "relation", relation)
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs, "edge.evidence_refs", item_limit=240))
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes, "edge.attributes"))

    @property
    def key(self) -> tuple[str, str, str]:
        return self.source_id, self.target_id, self.relation

    def to_record(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "evidence_refs": list(self.evidence_refs),
            "attributes": _thaw_json(self.attributes),
        }


@dataclass(frozen=True, slots=True, order=True, kw_only=True)
class StructuralIssue:
    """An explicit sensor limitation or unresolved structural finding."""

    code: str
    severity: str
    detail: str
    subject_id: str = ""
    target_id: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _text(self.code, "issue.code", limit=160))
        severity = _text(self.severity, "issue.severity", limit=40).upper()
        if severity not in STRUCTURAL_ISSUE_SEVERITIES:
            raise StructuralObservationError(f"지원하지 않는 issue severity입니다: {severity}")
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "detail", _text(self.detail, "issue.detail"))
        object.__setattr__(self, "subject_id", _text(self.subject_id, "issue.subject_id", limit=320, required=False))
        object.__setattr__(self, "target_id", _text(self.target_id, "issue.target_id", limit=320, required=False))
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs, "issue.evidence_refs", item_limit=240))

    def to_record(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "detail": self.detail,
            "subject_id": self.subject_id,
            "target_id": self.target_id,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralScope:
    """The bounded scope a sensor claims to have inspected."""

    root: str = "."
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_depth: int | None = None

    def __post_init__(self) -> None:
        root = _locator(self.root, "scope.root") or "."
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "include", tuple(path.replace("\\", "/") for path in _strings(self.include, "scope.include")))
        object.__setattr__(self, "exclude", tuple(path.replace("\\", "/") for path in _strings(self.exclude, "scope.exclude")))
        if self.max_depth is not None and (not isinstance(self.max_depth, int) or isinstance(self.max_depth, bool) or self.max_depth < 0):
            raise StructuralObservationError("scope.max_depth는 0 이상의 정수 또는 null이어야 합니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "max_depth": self.max_depth,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralObservation:
    """Immutable evidence returned by a structural sensor."""

    source_id: str
    sensor_type: str
    sensor_version: str
    workspace_hash: str
    nodes: tuple[StructuralNode, ...] = ()
    edges: tuple[StructuralEdge, ...] = ()
    issues: tuple[StructuralIssue, ...] = ()
    scope: StructuralScope = field(default_factory=StructuralScope)
    evidence: tuple[StructuralEvidence, ...] = ()
    unresolved: tuple[str, ...] = ()
    captured_at: str = field(default_factory=utc_now)
    schema_version: str = STRUCTURAL_OBSERVATION_SCHEMA
    id: str = ""
    observation_hash: str = ""
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        schema_version = _text(self.schema_version, "schema_version", limit=120)
        if schema_version != STRUCTURAL_OBSERVATION_SCHEMA:
            raise StructuralObservationError(f"지원하지 않는 observation schema입니다: {schema_version}")
        source_id = _text(self.source_id, "source_id", limit=240)
        sensor_type = _text(self.sensor_type, "sensor_type", limit=120).lower()
        sensor_version = _text(self.sensor_version, "sensor_version", limit=120)
        workspace_hash = _text(self.workspace_hash, "workspace_hash", limit=240)
        captured_at = _text(self.captured_at, "captured_at", limit=120)
        observation_id = _text(self.id, "id", limit=240, required=False)
        observation_hash = _text(self.observation_hash, "observation_hash", limit=240, required=False)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "sensor_type", sensor_type)
        object.__setattr__(self, "sensor_version", sensor_version)
        object.__setattr__(self, "workspace_hash", workspace_hash)
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(self, "id", observation_id)
        object.__setattr__(self, "observation_hash", observation_hash)
        if not isinstance(self.canonical_mutation, bool):
            raise StructuralObservationError("canonical_mutation은 boolean이어야 합니다.")
        if self.canonical_mutation:
            raise StructuralObservationError("StructuralObservation은 Canonical State를 변경할 수 없습니다.")
        if not isinstance(self.scope, StructuralScope):
            raise StructuralObservationError("scope는 StructuralScope여야 합니다.")

        nodes = tuple(
            sorted(
                _typed_items(self.nodes, "nodes", StructuralNode, _MAX_NODES),
                key=lambda node: node.id,
            )
        )
        edges = tuple(
            sorted(
                _typed_items(self.edges, "edges", StructuralEdge, _MAX_EDGES),
                key=lambda edge: edge.key,
            )
        )
        issues = tuple(
            sorted(
                _typed_items(self.issues, "issues", StructuralIssue, _MAX_ISSUES),
            )
        )
        evidence = tuple(
            sorted(
                _typed_items(self.evidence, "evidence", StructuralEvidence, _MAX_EVIDENCE),
                key=lambda item: item.id,
            )
        )
        unresolved = _strings(self.unresolved, "unresolved")
        if len(nodes) != len({node.id for node in nodes}):
            raise StructuralObservationError("StructuralObservation node id가 중복됩니다.")
        if len(edges) != len({edge.key for edge in edges}):
            raise StructuralObservationError("StructuralObservation edge가 중복됩니다.")
        if len(evidence) != len({item.id for item in evidence}):
            raise StructuralObservationError("StructuralObservation evidence id가 중복됩니다.")

        node_ids = {node.id for node in nodes}
        evidence_ids = {item.id for item in evidence}
        refs = {
            ref
            for node in nodes
            for ref in node.evidence_refs
        }
        refs.update(ref for edge in edges for ref in edge.evidence_refs)
        refs.update(ref for issue in issues for ref in issue.evidence_refs)
        missing_evidence = refs - evidence_ids
        if missing_evidence:
            raise StructuralObservationError(f"존재하지 않는 evidence를 참조합니다: {sorted(missing_evidence)}")

        missing_nodes = {
            endpoint
            for edge in edges
            for endpoint in (edge.source_id, edge.target_id)
            if endpoint not in node_ids
        }
        if missing_nodes and not self._missing_nodes_are_explicit(missing_nodes, issues, unresolved):
            raise StructuralObservationError(
                "missing endpoint는 issue 또는 unresolved에 명시해야 합니다: "
                f"{sorted(missing_nodes)}"
            )

        object.__setattr__(self, "sensor_type", self.sensor_type.strip().lower())
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "unresolved", unresolved)
        computed = canonical_hash(self._hash_payload())
        if observation_hash and observation_hash != computed:
            raise StructuralObservationError("observation_hash가 payload와 일치하지 않습니다.")
        expected_id = f"structural-observation:{computed.removeprefix('sha256:')}"
        if observation_id and observation_id != expected_id:
            raise StructuralObservationError("StructuralObservation id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "observation_hash", computed)
        object.__setattr__(self, "id", expected_id)

    @staticmethod
    def _missing_nodes_are_explicit(
        missing_nodes: set[str],
        issues: Sequence[StructuralIssue],
        unresolved: Sequence[str],
    ) -> bool:
        unresolved_markers = set(unresolved)
        for node_id in missing_nodes:
            if f"missing_node:{node_id}" in unresolved_markers:
                continue
            if any(
                issue.code == "missing_endpoint"
                and node_id in {issue.subject_id, issue.target_id}
                for issue in issues
            ):
                continue
            return False
        return True

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "sensor_type": self.sensor_type.strip().lower(),
            "sensor_version": self.sensor_version,
            "workspace_hash": self.workspace_hash,
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "issues": [issue.to_record() for issue in self.issues],
            "scope": self.scope.to_record(),
            "evidence": [item.to_record() for item in self.evidence],
            "unresolved": list(self.unresolved),
        }

    @property
    def missing_endpoints(self) -> tuple[str, ...]:
        node_ids = {node.id for node in self.nodes}
        return tuple(
            sorted(
                {
                    endpoint
                    for edge in self.edges
                    for endpoint in (edge.source_id, edge.target_id)
                    if endpoint not in node_ids
                }
            )
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "source_id": self.source_id,
            "sensor_type": self.sensor_type,
            "sensor_version": self.sensor_version,
            "workspace_hash": self.workspace_hash,
            "captured_at": self.captured_at,
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "issues": [issue.to_record() for issue in self.issues],
            "scope": self.scope.to_record(),
            "evidence": [item.to_record() for item in self.evidence],
            "unresolved": list(self.unresolved),
            "observation_hash": self.observation_hash,
            "canonical_mutation": False,
        }


def build_structural_observation(
    *,
    source_id: str,
    sensor_type: str,
    sensor_version: str,
    workspace_hash: str,
    nodes: Sequence[StructuralNode] = (),
    edges: Sequence[StructuralEdge] = (),
    issues: Sequence[StructuralIssue] = (),
    scope: StructuralScope | None = None,
    evidence: Sequence[StructuralEvidence] = (),
    unresolved: Sequence[str] = (),
    captured_at: str | None = None,
) -> StructuralObservation:
    """Build one normalized observation without performing any I/O."""

    return StructuralObservation(
        source_id=source_id,
        sensor_type=sensor_type,
        sensor_version=sensor_version,
        workspace_hash=workspace_hash,
        nodes=tuple(nodes),
        edges=tuple(edges),
        issues=tuple(issues),
        scope=scope or StructuralScope(),
        evidence=tuple(evidence),
        unresolved=tuple(unresolved),
        captured_at=captured_at or utc_now(),
    )


_ROOT_KEYS = {
    "schema_version",
    "id",
    "source_id",
    "sensor_type",
    "sensor_version",
    "workspace_hash",
    "captured_at",
    "nodes",
    "edges",
    "issues",
    "scope",
    "evidence",
    "unresolved",
    "observation_hash",
    "canonical_mutation",
}
_NODE_KEYS = {"id", "kind", "label", "locator", "attributes", "evidence_refs"}
_EDGE_KEYS = {"source_id", "target_id", "relation", "evidence_refs", "attributes"}
_ISSUE_KEYS = {"code", "severity", "detail", "subject_id", "target_id", "evidence_refs"}
_SCOPE_KEYS = {"root", "include", "exclude", "max_depth"}
_EVIDENCE_KEYS = {"id", "source_id", "kind", "locator", "digest", "metadata"}


def _typed_items(value: Any, label: str, expected_type: type[Any], max_items: int) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructuralObservationError(f"{label}는 배열이어야 합니다.")
    if len(value) > max_items:
        raise StructuralObservationError(f"{label} 항목이 너무 많습니다.")
    result = tuple(value)
    if any(not isinstance(item, expected_type) for item in result):
        raise StructuralObservationError(
            f"{label}에는 {expected_type.__name__} 항목만 허용됩니다."
        )
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructuralObservationError(f"{label}는 JSON 객체여야 합니다.")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise StructuralObservationError(f"{label}는 JSON 배열이어야 합니다.")
    return value


def _parse_node(value: Any, index: int) -> StructuralNode:
    record = _mapping(value, f"nodes[{index}]")
    _strict_keys(record, _NODE_KEYS, f"nodes[{index}]", {"id", "kind", "label"})
    return StructuralNode(
        id=record["id"],
        kind=record["kind"],
        label=record["label"],
        locator=record.get("locator", ""),
        attributes=record.get("attributes", {}),
        evidence_refs=record.get("evidence_refs", []),
    )


def _parse_edge(value: Any, index: int) -> StructuralEdge:
    record = _mapping(value, f"edges[{index}]")
    _strict_keys(record, _EDGE_KEYS, f"edges[{index}]", {"source_id", "target_id", "relation"})
    return StructuralEdge(
        source_id=record["source_id"],
        target_id=record["target_id"],
        relation=record["relation"],
        evidence_refs=record.get("evidence_refs", []),
        attributes=record.get("attributes", {}),
    )


def _parse_issue(value: Any, index: int) -> StructuralIssue:
    record = _mapping(value, f"issues[{index}]")
    _strict_keys(record, _ISSUE_KEYS, f"issues[{index}]", {"code", "severity", "detail"})
    return StructuralIssue(
        code=record["code"],
        severity=record["severity"],
        detail=record["detail"],
        subject_id=record.get("subject_id", ""),
        target_id=record.get("target_id", ""),
        evidence_refs=record.get("evidence_refs", []),
    )


def _parse_scope(value: Any) -> StructuralScope:
    record = _mapping(value, "scope")
    _strict_keys(record, _SCOPE_KEYS, "scope")
    return StructuralScope(
        root=record.get("root", "."),
        include=record.get("include", []),
        exclude=record.get("exclude", []),
        max_depth=record.get("max_depth"),
    )


def _parse_evidence(value: Any, index: int) -> StructuralEvidence:
    record = _mapping(value, f"evidence[{index}]")
    _strict_keys(record, _EVIDENCE_KEYS, f"evidence[{index}]", {"id", "source_id", "kind"})
    return StructuralEvidence(
        id=record["id"],
        source_id=record["source_id"],
        kind=record.get("kind", "sensor_output"),
        locator=record.get("locator", ""),
        digest=record.get("digest"),
        metadata=record.get("metadata", {}),
    )


def parse_structural_observation(value: Mapping[str, Any] | str) -> StructuralObservation:
    """Parse exactly one strict observation record or JSON document."""

    if isinstance(value, str):
        if len(value.encode("utf-8")) > 8 * 1024 * 1024:
            raise StructuralObservationError("observation JSON이 너무 큽니다.")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise StructuralObservationError("observation은 JSON 객체여야 합니다.") from exc
    record = _mapping(value, "observation")
    _strict_keys(
        record,
        _ROOT_KEYS,
        "observation",
        {"schema_version", "source_id", "sensor_type", "sensor_version", "workspace_hash", "captured_at", "nodes", "edges", "issues", "scope", "evidence", "unresolved"},
    )
    canonical_mutation = record.get("canonical_mutation", False)
    if not isinstance(canonical_mutation, bool):
        raise StructuralObservationError("canonical_mutation은 boolean이어야 합니다.")
    return StructuralObservation(
        schema_version=record["schema_version"],
        id=record.get("id", ""),
        source_id=record["source_id"],
        sensor_type=record["sensor_type"],
        sensor_version=record["sensor_version"],
        workspace_hash=record["workspace_hash"],
        captured_at=record["captured_at"],
        nodes=tuple(_parse_node(item, index) for index, item in enumerate(_list(record["nodes"], "nodes"))),
        edges=tuple(_parse_edge(item, index) for index, item in enumerate(_list(record["edges"], "edges"))),
        issues=tuple(_parse_issue(item, index) for index, item in enumerate(_list(record["issues"], "issues"))),
        scope=_parse_scope(record["scope"]),
        evidence=tuple(_parse_evidence(item, index) for index, item in enumerate(_list(record["evidence"], "evidence"))),
        unresolved=record["unresolved"],
        observation_hash=record.get("observation_hash", ""),
        canonical_mutation=canonical_mutation,
    )


__all__ = [
    "STRUCTURAL_ISSUE_SEVERITIES",
    "STRUCTURAL_OBSERVATION_SCHEMA",
    "STRUCTURAL_RELATIONS",
    "StructuralEdge",
    "StructuralEvidence",
    "StructuralIssue",
    "StructuralNode",
    "StructuralObservation",
    "StructuralObservationError",
    "StructuralScope",
    "build_structural_observation",
    "parse_structural_observation",
]
