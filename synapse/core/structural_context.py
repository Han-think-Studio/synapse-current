"""Bounded, read-only context projection for structural evidence.

This module selects a small, explicitly requested neighborhood from one
``StructuralObservation``.  It is intentionally downstream-only: the source
observation is never changed, no executor is called, and no state or
filesystem boundary is crossed.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_observation import (
    STRUCTURAL_RELATIONS,
    StructuralEdge,
    StructuralEvidence,
    StructuralIssue,
    StructuralObservation,
)


class StructuralContextError(ValueError):
    """Raised when a bounded structural context cannot be built safely."""


STRUCTURAL_CONTEXT_SCHEMA = "structural.context.v1"
STRUCTURAL_CONTEXT_DIRECTIONS = frozenset({"outbound", "inbound", "both"})
STRUCTURAL_CONTEXT_EXCLUSION_REASONS = frozenset(
    {"relation_not_allowed", "depth_limit", "max_nodes", "unresolved_endpoint"}
)
_MAX_DEPTH = 100
_MAX_NODES = 10_000
_MAX_EDGES = 50_000
_MAX_ISSUES = 20_000
_MAX_EXCLUSIONS = 50_000
_MAX_EVIDENCE = 50_000
_MAX_UNRESOLVED = 20_000
_MAX_TEXT = 4_000
_MAX_ATTRIBUTE_DEPTH = 12


def _text(value: Any, label: str, *, limit: int = _MAX_TEXT, required: bool = True) -> str:
    if not isinstance(value, str):
        raise StructuralContextError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise StructuralContextError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise StructuralContextError(f"{label}이(가) 너무 깁니다.")
    return result


def _strings(
    value: Any,
    label: str,
    *,
    max_items: int = _MAX_UNRESOLVED,
    item_limit: int = 1_000,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructuralContextError(f"{label}는 문자열 배열이어야 합니다.")
    if len(value) > max_items:
        raise StructuralContextError(f"{label} 항목이 너무 많습니다.")
    result = tuple(_text(item, label, limit=item_limit) for item in value)
    if len(result) != len(set(result)):
        raise StructuralContextError(f"{label}에 중복 항목이 있습니다.")
    return tuple(sorted(result))


def _relation_tokens(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructuralContextError(f"{label}는 relation 문자열 배열이어야 합니다.")
    if not value:
        raise StructuralContextError(f"{label}은(는) 하나 이상 필요합니다.")
    normalized = tuple(_text(item, label, limit=80).upper() for item in value)
    if len(normalized) != len(set(normalized)):
        raise StructuralContextError(f"{label}에 중복 relation이 있습니다.")
    unsupported = set(normalized) - STRUCTURAL_RELATIONS
    if unsupported:
        raise StructuralContextError(f"지원하지 않는 structural relation입니다: {sorted(unsupported)}")
    return tuple(sorted(normalized))


def _freeze_json(value: Any, label: str, *, depth: int = 0) -> Any:
    if depth > _MAX_ATTRIBUTE_DEPTH:
        raise StructuralContextError(f"{label} 중첩 깊이가 너무 깊습니다.")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructuralContextError(f"{label}에 유한하지 않은 숫자가 있습니다.")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise StructuralContextError(f"{label}의 객체 키는 문자열이어야 합니다.")
            frozen[key] = _freeze_json(item, f"{label}.{key}", depth=depth + 1)
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, label, depth=depth + 1) for item in value)
    raise StructuralContextError(f"{label}에는 JSON 값만 허용됩니다.")


def _freeze_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructuralContextError(f"{label}는 JSON 객체여야 합니다.")
    frozen = _freeze_json(value, label)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded by _freeze_json
        raise StructuralContextError(f"{label}는 JSON 객체여야 합니다.")
    return frozen


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _typed_items(
    value: Any,
    label: str,
    expected_type: type[Any],
    max_items: int,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructuralContextError(f"{label}는 배열이어야 합니다.")
    if len(value) > max_items:
        raise StructuralContextError(f"{label} 항목이 너무 많습니다.")
    result = tuple(value)
    if any(not isinstance(item, expected_type) for item in result):
        raise StructuralContextError(f"{label}에는 {expected_type.__name__} 항목만 허용됩니다.")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralContextRequest:
    """Explicit selection and freshness binding for one bounded context."""

    target_id: str
    source_hash: str
    relations: tuple[str, ...]
    max_depth: int = 1
    max_nodes: int = 100
    direction: str = "both"
    schema_version: str = STRUCTURAL_CONTEXT_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _text(self.schema_version, "request.schema_version", limit=120)
        if schema_version != STRUCTURAL_CONTEXT_SCHEMA:
            raise StructuralContextError(f"지원하지 않는 structural context schema입니다: {schema_version}")
        target_id = _text(self.target_id, "request.target_id", limit=500)
        source_hash = _text(self.source_hash, "request.source_hash", limit=240)
        relations = _relation_tokens(self.relations, "request.relations")
        direction = _text(self.direction, "request.direction", limit=40).lower()
        if direction not in STRUCTURAL_CONTEXT_DIRECTIONS:
            raise StructuralContextError(f"지원하지 않는 context direction입니다: {direction}")
        if (
            not isinstance(self.max_depth, int)
            or isinstance(self.max_depth, bool)
            or self.max_depth < 0
            or self.max_depth > _MAX_DEPTH
        ):
            raise StructuralContextError(f"request.max_depth는 0~{_MAX_DEPTH} 정수여야 합니다.")
        if (
            not isinstance(self.max_nodes, int)
            or isinstance(self.max_nodes, bool)
            or self.max_nodes < 1
            or self.max_nodes > _MAX_NODES
        ):
            raise StructuralContextError(f"request.max_nodes는 1~{_MAX_NODES} 정수여야 합니다.")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "relations", relations)
        object.__setattr__(self, "direction", direction)

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_id": self.target_id,
            "source_hash": self.source_hash,
            "relations": list(self.relations),
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "direction": self.direction,
        }


@dataclass(frozen=True, slots=True, order=True, kw_only=True)
class StructuralContextNode:
    """One source node selected into the bounded context."""

    id: str
    kind: str
    label: str
    depth: int
    locator: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict, compare=False)
    evidence_refs: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "context.node.id", limit=500))
        object.__setattr__(self, "kind", _text(self.kind, "context.node.kind", limit=160))
        object.__setattr__(self, "label", _text(self.label, "context.node.label"))
        object.__setattr__(self, "locator", _text(self.locator, "context.node.locator", required=False))
        if not isinstance(self.depth, int) or isinstance(self.depth, bool) or self.depth < 0:
            raise StructuralContextError("context.node.depth는 0 이상의 정수여야 합니다.")
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes, "context.node.attributes"))
        object.__setattr__(
            self,
            "evidence_refs",
            _strings(self.evidence_refs, "context.node.evidence_refs", item_limit=240),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "depth": self.depth,
            "locator": self.locator,
            "attributes": _thaw_json(self.attributes),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True, order=True, kw_only=True)
class StructuralContextEdge:
    """One directed source edge whose endpoints fit the bounded context."""

    source_id: str
    target_id: str
    relation: str
    evidence_refs: tuple[str, ...] = field(default=(), compare=False)
    attributes: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _text(self.source_id, "context.edge.source_id", limit=500))
        object.__setattr__(self, "target_id", _text(self.target_id, "context.edge.target_id", limit=500))
        relation = _text(self.relation, "context.edge.relation", limit=80).upper()
        if relation not in STRUCTURAL_RELATIONS:
            raise StructuralContextError(f"지원하지 않는 structural relation입니다: {relation}")
        object.__setattr__(self, "relation", relation)
        object.__setattr__(
            self,
            "evidence_refs",
            _strings(self.evidence_refs, "context.edge.evidence_refs", item_limit=240),
        )
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes, "context.edge.attributes"))

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
class StructuralContextExclusion:
    """Why a reachable edge/endpoint was not included in the bounded view."""

    reason: str
    detail: str
    source_id: str = ""
    target_id: str = ""
    relation: str = ""
    evidence_refs: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        reason = _text(self.reason, "exclusion.reason", limit=80).lower()
        if reason not in STRUCTURAL_CONTEXT_EXCLUSION_REASONS:
            raise StructuralContextError(f"지원하지 않는 context exclusion reason입니다: {reason}")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "detail", _text(self.detail, "exclusion.detail"))
        object.__setattr__(self, "source_id", _text(self.source_id, "exclusion.source_id", limit=500, required=False))
        object.__setattr__(self, "target_id", _text(self.target_id, "exclusion.target_id", limit=500, required=False))
        relation = _text(self.relation, "exclusion.relation", limit=80, required=False)
        if relation:
            relation = relation.upper()
            if relation not in STRUCTURAL_RELATIONS:
                raise StructuralContextError(f"지원하지 않는 structural relation입니다: {relation}")
        object.__setattr__(self, "relation", relation)
        object.__setattr__(
            self,
            "evidence_refs",
            _strings(self.evidence_refs, "exclusion.evidence_refs", item_limit=240),
        )

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return self.source_id, self.target_id, self.relation, self.reason, self.detail

    def to_record(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralContext:
    """Immutable, bounded, non-authoritative structural context."""

    source_id: str
    source_hash: str
    request: StructuralContextRequest
    nodes: tuple[StructuralContextNode, ...] = ()
    edges: tuple[StructuralContextEdge, ...] = ()
    issues: tuple[StructuralIssue, ...] = ()
    exclusions: tuple[StructuralContextExclusion, ...] = ()
    evidence: tuple[StructuralEvidence, ...] = ()
    unresolved: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = STRUCTURAL_CONTEXT_SCHEMA
    id: str = ""
    context_hash: str = ""
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        schema_version = _text(self.schema_version, "schema_version", limit=120)
        if schema_version != STRUCTURAL_CONTEXT_SCHEMA:
            raise StructuralContextError(f"지원하지 않는 structural context schema입니다: {schema_version}")
        source_id = _text(self.source_id, "source_id", limit=500)
        source_hash = _text(self.source_hash, "source_hash", limit=240)
        if not isinstance(self.request, StructuralContextRequest):
            raise StructuralContextError("context.request 타입이 잘못되었습니다.")
        if self.request.source_hash != source_hash:
            raise StructuralContextError("context request source_hash가 context와 다릅니다.")
        if not isinstance(self.canonical_mutation, bool):
            raise StructuralContextError("canonical_mutation은 boolean이어야 합니다.")
        if not isinstance(self.filesystem_mutation, bool):
            raise StructuralContextError("filesystem_mutation은 boolean이어야 합니다.")
        if self.canonical_mutation or self.filesystem_mutation:
            raise StructuralContextError("StructuralContext는 상태나 파일을 변경할 수 없습니다.")

        nodes = tuple(sorted(_typed_items(self.nodes, "nodes", StructuralContextNode, _MAX_NODES), key=lambda item: item.id))
        edges = tuple(sorted(_typed_items(self.edges, "edges", StructuralContextEdge, _MAX_EDGES), key=lambda item: item.key))
        issues = tuple(sorted(_typed_items(self.issues, "issues", StructuralIssue, _MAX_ISSUES)))
        exclusions = tuple(
            sorted(
                _typed_items(self.exclusions, "exclusions", StructuralContextExclusion, _MAX_EXCLUSIONS),
                key=lambda item: item.key,
            )
        )
        evidence = tuple(sorted(_typed_items(self.evidence, "evidence", StructuralEvidence, _MAX_EVIDENCE), key=lambda item: item.id))
        unresolved = _strings(self.unresolved, "unresolved")
        metadata = _freeze_mapping(self.metadata, "metadata")

        if len(nodes) != len({node.id for node in nodes}):
            raise StructuralContextError("context node id가 중복됩니다.")
        if len(edges) != len({edge.key for edge in edges}):
            raise StructuralContextError("context edge가 중복됩니다.")
        if len(exclusions) != len({item.key for item in exclusions}):
            raise StructuralContextError("context exclusion이 중복됩니다.")
        if len(evidence) != len({item.id for item in evidence}):
            raise StructuralContextError("context evidence id가 중복됩니다.")
        if len(nodes) > self.request.max_nodes:
            raise StructuralContextError("context node가 요청한 max_nodes를 초과했습니다.")
        if any(node.depth > self.request.max_depth for node in nodes):
            raise StructuralContextError("context node가 요청한 max_depth를 초과했습니다.")

        node_ids = {node.id for node in nodes}
        if self.request.target_id not in node_ids:
            raise StructuralContextError("context target node가 결과에 포함되어야 합니다.")
        unsupported_edges = {
            edge.relation for edge in edges if edge.relation not in self.request.relations
        }
        if unsupported_edges:
            raise StructuralContextError(
                f"context edge가 요청 relation allowlist를 벗어났습니다: {sorted(unsupported_edges)}"
            )
        missing_endpoints = {
            endpoint
            for edge in edges
            for endpoint in (edge.source_id, edge.target_id)
            if endpoint not in node_ids
        }
        if missing_endpoints - set(unresolved):
            raise StructuralContextError("context missing endpoint는 unresolved에 명시해야 합니다.")

        evidence_ids = {item.id for item in evidence}
        refs = {
            ref
            for node in nodes
            for ref in node.evidence_refs
        }
        refs.update(ref for edge in edges for ref in edge.evidence_refs)
        refs.update(ref for issue in issues for ref in issue.evidence_refs)
        refs.update(ref for exclusion in exclusions for ref in exclusion.evidence_refs)
        missing_evidence = refs - evidence_ids
        if missing_evidence:
            raise StructuralContextError(f"존재하지 않는 context evidence를 참조합니다: {sorted(missing_evidence)}")

        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "exclusions", exclusions)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "unresolved", unresolved)
        object.__setattr__(self, "metadata", metadata)

        computed = canonical_hash(self._hash_payload())
        if self.context_hash and self.context_hash != computed:
            raise StructuralContextError("context_hash가 payload와 일치하지 않습니다.")
        context_id = _text(self.id, "id", limit=500, required=False)
        expected_id = f"structural-context:{computed.removeprefix('sha256:')}"
        if context_id and context_id != expected_id:
            raise StructuralContextError("StructuralContext id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "context_hash", computed)
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, Any]:
        metadata = _thaw_json(self.metadata)
        if isinstance(metadata, dict):
            metadata.pop("captured_at", None)
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "request": self.request.to_record(),
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "issues": [issue.to_record() for issue in self.issues],
            "exclusions": [item.to_record() for item in self.exclusions],
            "evidence": [item.to_record() for item in self.evidence],
            "unresolved": list(self.unresolved),
            "metadata": metadata,
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "request": self.request.to_record(),
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "issues": [issue.to_record() for issue in self.issues],
            "exclusions": [item.to_record() for item in self.exclusions],
            "evidence": [item.to_record() for item in self.evidence],
            "unresolved": list(self.unresolved),
            "metadata": _thaw_json(self.metadata),
            "context_hash": self.context_hash,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        import json

        options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
        if indent is None:
            options["separators"] = (",", ":")
        else:
            options["indent"] = indent
        return json.dumps(self.to_record(), **options)


def _edge_is_traversable(edge: StructuralEdge, direction: str, current_id: str) -> tuple[str, ...]:
    if direction == "outbound" and edge.source_id == current_id:
        return (edge.target_id,)
    if direction == "inbound" and edge.target_id == current_id:
        return (edge.source_id,)
    if direction == "both":
        neighbors: list[str] = []
        if edge.source_id == current_id:
            neighbors.append(edge.target_id)
        if edge.target_id == current_id and edge.source_id not in neighbors:
            neighbors.append(edge.source_id)
        return tuple(neighbors)
    return ()


def build_bounded_structural_context(
    observation: StructuralObservation,
    request: StructuralContextRequest,
) -> StructuralContext:
    """Build a deterministic bounded neighborhood from one observation."""

    if not isinstance(observation, StructuralObservation):
        raise StructuralContextError("StructuralObservation만 context source로 사용할 수 있습니다.")
    if not isinstance(request, StructuralContextRequest):
        raise StructuralContextError("StructuralContextRequest가 필요합니다.")
    if request.source_hash != observation.observation_hash:
        raise StructuralContextError("context source hash가 현재 observation과 일치하지 않습니다.")

    source_nodes = {node.id: node for node in observation.nodes}
    if request.target_id not in source_nodes:
        raise StructuralContextError(f"context target node를 찾을 수 없습니다: {request.target_id}")

    adjacency: dict[str, list[tuple[str, StructuralEdge]]] = {}
    for edge in sorted(observation.edges, key=lambda item: item.key):
        for neighbor in _edge_is_traversable(edge, request.direction, edge.source_id):
            adjacency.setdefault(edge.source_id, []).append((neighbor, edge))
        for neighbor in _edge_is_traversable(edge, request.direction, edge.target_id):
            adjacency.setdefault(edge.target_id, []).append((neighbor, edge))
    for values in adjacency.values():
        values.sort(key=lambda item: (item[1].key, item[0]))

    selected_depth: dict[str, int] = {request.target_id: 0}
    queue: deque[str] = deque((request.target_id,))
    exclusions: list[StructuralContextExclusion] = []
    exclusion_keys: set[tuple[str, str, str, str, str]] = set()
    missing_endpoints: set[str] = set()

    def add_exclusion(
        *,
        reason: str,
        detail: str,
        edge: StructuralEdge | None = None,
        target_id: str = "",
    ) -> None:
        source_id = edge.source_id if edge else ""
        relation = edge.relation if edge else ""
        resolved_target = target_id or (edge.target_id if edge else "")
        item = StructuralContextExclusion(
            reason=reason,
            detail=detail,
            source_id=source_id,
            target_id=resolved_target,
            relation=relation,
            evidence_refs=edge.evidence_refs if edge else (),
        )
        if item.key in exclusion_keys:
            return
        if len(exclusions) >= _MAX_EXCLUSIONS:
            raise StructuralContextError("context exclusions가 허용 한도를 초과했습니다.")
        exclusion_keys.add(item.key)
        exclusions.append(item)

    while queue:
        current_id = queue.popleft()
        current_depth = selected_depth[current_id]
        for neighbor_id, edge in adjacency.get(current_id, ()):
            if edge.relation not in request.relations:
                add_exclusion(
                    reason="relation_not_allowed",
                    detail=f"{edge.relation} relation은 요청 allowlist에 없습니다.",
                    edge=edge,
                    target_id=neighbor_id,
                )
                continue
            if neighbor_id not in source_nodes:
                missing_endpoints.add(neighbor_id)
                add_exclusion(
                    reason="unresolved_endpoint",
                    detail=f"관측에 node가 없어 unresolved로 남깁니다: {neighbor_id}",
                    edge=edge,
                    target_id=neighbor_id,
                )
                continue
            if neighbor_id in selected_depth:
                continue
            next_depth = current_depth + 1
            if next_depth > request.max_depth:
                add_exclusion(
                    reason="depth_limit",
                    detail=f"요청 max_depth={request.max_depth}를 넘었습니다.",
                    edge=edge,
                    target_id=neighbor_id,
                )
                continue
            if len(selected_depth) >= request.max_nodes:
                add_exclusion(
                    reason="max_nodes",
                    detail=f"요청 max_nodes={request.max_nodes}에 도달했습니다.",
                    edge=edge,
                    target_id=neighbor_id,
                )
                continue
            selected_depth[neighbor_id] = next_depth
            queue.append(neighbor_id)

    selected_ids = set(selected_depth)

    def edge_fits_direction(edge: StructuralEdge) -> bool:
        if edge.source_id not in selected_ids or edge.target_id not in selected_ids:
            return False
        if request.direction == "outbound":
            return edge.source_id in selected_ids and edge.target_id in selected_ids
        if request.direction == "inbound":
            return edge.target_id in selected_ids and edge.source_id in selected_ids
        return True

    included_edges = tuple(
        edge
        for edge in sorted(observation.edges, key=lambda item: item.key)
        if edge.relation in request.relations and edge_fits_direction(edge)
    )
    context_nodes = tuple(
        StructuralContextNode(
            id=node.id,
            kind=node.kind,
            label=node.label,
            depth=selected_depth[node.id],
            locator=node.locator,
            attributes=node.attributes,
            evidence_refs=node.evidence_refs,
        )
        for node in sorted(
            (source_nodes[node_id] for node_id in selected_ids),
            key=lambda item: item.id,
        )
    )
    context_edges = tuple(
        StructuralContextEdge(
            source_id=edge.source_id,
            target_id=edge.target_id,
            relation=edge.relation,
            evidence_refs=edge.evidence_refs,
            attributes=edge.attributes,
        )
        for edge in included_edges
    )
    relevant_ids = selected_ids | missing_endpoints
    context_issues = tuple(
        issue
        for issue in observation.issues
        if not issue.subject_id
        and not issue.target_id
        or issue.subject_id in relevant_ids
        or issue.target_id in relevant_ids
    )
    unresolved = tuple(sorted(set(observation.unresolved) | {f"missing_node:{item}" for item in missing_endpoints}))

    evidence_ids: set[str] = set()
    evidence_ids.update(ref for node in context_nodes for ref in node.evidence_refs)
    evidence_ids.update(ref for edge in context_edges for ref in edge.evidence_refs)
    evidence_ids.update(ref for issue in context_issues for ref in issue.evidence_refs)
    evidence_ids.update(ref for item in exclusions for ref in item.evidence_refs)
    context_evidence = tuple(item for item in observation.evidence if item.id in evidence_ids)
    metadata = {
        "observation_source_id": observation.source_id,
        "sensor_type": observation.sensor_type,
        "sensor_version": observation.sensor_version,
        "workspace_hash": observation.workspace_hash,
        "captured_at": observation.captured_at,
        "scope": observation.scope.to_record(),
    }
    return StructuralContext(
        source_id=observation.id,
        source_hash=observation.observation_hash,
        request=request,
        nodes=context_nodes,
        edges=context_edges,
        issues=context_issues,
        exclusions=tuple(exclusions),
        evidence=context_evidence,
        unresolved=unresolved,
        metadata=metadata,
    )


__all__ = [
    "STRUCTURAL_CONTEXT_DIRECTIONS",
    "STRUCTURAL_CONTEXT_EXCLUSION_REASONS",
    "STRUCTURAL_CONTEXT_SCHEMA",
    "StructuralContext",
    "StructuralContextEdge",
    "StructuralContextError",
    "StructuralContextExclusion",
    "StructuralContextNode",
    "StructuralContextRequest",
    "build_bounded_structural_context",
]
