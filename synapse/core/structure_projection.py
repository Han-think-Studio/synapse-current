"""Read-only structure projections for the Phase 30 contract.

This module renders existing deterministic structures for human inspection. It
does not edit its inputs, infer Canonical State, call a model, or write a
workspace. The three adapters deliberately retain the distinction between a
source structure and its downstream visual projection.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.graph import DependencyGraph, GraphEdge, GraphIssue
from synapse.core.idea_session import OpenQuestion, canonical_hash
from synapse.core.ir import IRIssue, IRNode, IRRelation, SynapseIR
from synapse.core.reactive import ReactiveTrace
from synapse.core.structural_observation import StructuralObservation
from synapse.core.structure_map import (
    StructureEdge,
    StructureMapProposal,
    StructureNode,
    WorkUnit,
)


class StructureProjectionError(ValueError):
    """Raised when a read-only structure projection cannot be built safely."""


STRUCTURE_PROJECTION_SCHEMA = "structure.projection.v1"
STRUCTURE_PROJECTION_SOURCE_KINDS = frozenset(
    {"dependency_graph", "structure_map", "reactive_trace", "structural_observation"}
)
_MAX_NODES = 50_000
_MAX_EDGES = 100_000
_MAX_ISSUES = 20_000
_MAX_UNRESOLVED = 20_000
_MAX_ATTRIBUTE_DEPTH = 10


def _text(value: Any, label: str, *, limit: int = 4_000, required: bool = True) -> str:
    if not isinstance(value, str):
        raise StructureProjectionError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise StructureProjectionError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise StructureProjectionError(f"{label}이(가) 너무 깁니다.")
    return result


def _strings(value: Any, label: str, *, item_limit: int = 1_000) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructureProjectionError(f"{label}는 문자열 배열이어야 합니다.")
    result = tuple(_text(item, label, limit=item_limit) for item in value)
    if len(result) != len(set(result)):
        raise StructureProjectionError(f"{label}에 중복 항목이 있습니다.")
    return tuple(sorted(result))


def _freeze_json(value: Any, label: str, *, depth: int = 0) -> Any:
    if depth > _MAX_ATTRIBUTE_DEPTH:
        raise StructureProjectionError(f"{label} 중첩 깊이가 너무 깊습니다.")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructureProjectionError(f"{label}에 유한하지 않은 숫자가 있습니다.")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise StructureProjectionError(f"{label}의 객체 키는 문자열이어야 합니다.")
            frozen[key] = _freeze_json(item, f"{label}.{key}", depth=depth + 1)
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, label, depth=depth + 1) for item in value)
    raise StructureProjectionError(f"{label}에는 JSON 값만 허용됩니다.")


def _freeze_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructureProjectionError(f"{label}는 JSON 객체여야 합니다.")
    frozen = _freeze_json(value, label)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded by _freeze_json
        raise StructureProjectionError(f"{label}는 JSON 객체여야 합니다.")
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
        raise StructureProjectionError(f"{label}는 배열이어야 합니다.")
    if len(value) > max_items:
        raise StructureProjectionError(f"{label} 항목이 너무 많습니다.")
    result = tuple(value)
    if any(not isinstance(item, expected_type) for item in result):
        raise StructureProjectionError(
            f"{label}에는 {expected_type.__name__} 항목만 허용됩니다."
        )
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureProjectionNode:
    """One node in a downstream, non-authoritative structure view."""

    id: str
    kind: str
    label: str
    status: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "projection.node.id", limit=500))
        object.__setattr__(self, "kind", _text(self.kind, "projection.node.kind", limit=160))
        object.__setattr__(self, "label", _text(self.label, "projection.node.label"))
        object.__setattr__(
            self,
            "status",
            _text(self.status, "projection.node.status", limit=120, required=False),
        )
        object.__setattr__(
            self,
            "attributes",
            _freeze_mapping(self.attributes, "projection.node.attributes"),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "attributes": _thaw_json(self.attributes),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureProjectionEdge:
    """One directed relationship in a downstream structure view."""

    source_id: str
    target_id: str
    relation: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _text(self.source_id, "projection.edge.source_id", limit=500))
        object.__setattr__(self, "target_id", _text(self.target_id, "projection.edge.target_id", limit=500))
        object.__setattr__(self, "relation", _text(self.relation, "projection.edge.relation", limit=160))
        object.__setattr__(
            self,
            "attributes",
            _freeze_mapping(self.attributes, "projection.edge.attributes"),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return self.source_id, self.target_id, self.relation

    def to_record(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "attributes": _thaw_json(self.attributes),
        }


@dataclass(frozen=True, slots=True, order=True, kw_only=True)
class StructureProjectionIssue:
    """A source issue retained in the read-only projection."""

    code: str
    detail: str
    subject_id: str = ""
    target_id: str = ""
    severity: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _text(self.code, "projection.issue.code", limit=160))
        object.__setattr__(self, "detail", _text(self.detail, "projection.issue.detail"))
        object.__setattr__(
            self,
            "subject_id",
            _text(self.subject_id, "projection.issue.subject_id", limit=500, required=False),
        )
        object.__setattr__(
            self,
            "target_id",
            _text(self.target_id, "projection.issue.target_id", limit=500, required=False),
        )
        severity = _text(self.severity, "projection.issue.severity", limit=40, required=False)
        object.__setattr__(self, "severity", severity.upper())
        object.__setattr__(
            self,
            "evidence_refs",
            _strings(self.evidence_refs, "projection.issue.evidence_refs", item_limit=240),
        )

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "code": self.code,
            "detail": self.detail,
            "subject_id": self.subject_id,
            "target_id": self.target_id,
        }
        # Keep the Phase 30 record shape stable for existing sources while
        # retaining richer observation provenance when it is available.
        if self.severity:
            record["severity"] = self.severity
        if self.evidence_refs:
            record["evidence_refs"] = list(self.evidence_refs)
        return record


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureProjection:
    """Immutable JSON/Mermaid view derived from one existing structure."""

    source_kind: str
    source_id: str
    source_hash: str
    nodes: tuple[StructureProjectionNode, ...] = ()
    edges: tuple[StructureProjectionEdge, ...] = ()
    issues: tuple[StructureProjectionIssue, ...] = ()
    unresolved: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = STRUCTURE_PROJECTION_SCHEMA
    id: str = ""
    projection_hash: str = ""
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        schema_version = _text(self.schema_version, "schema_version", limit=120)
        if schema_version != STRUCTURE_PROJECTION_SCHEMA:
            raise StructureProjectionError(f"지원하지 않는 projection schema입니다: {schema_version}")
        source_kind = _text(self.source_kind, "source_kind", limit=80).lower()
        if source_kind not in STRUCTURE_PROJECTION_SOURCE_KINDS:
            raise StructureProjectionError(f"지원하지 않는 projection source kind입니다: {source_kind}")
        source_id = _text(self.source_id, "source_id", limit=500)
        source_hash = _text(self.source_hash, "source_hash", limit=240)
        projection_id = _text(self.id, "id", limit=500, required=False)
        projection_hash = _text(self.projection_hash, "projection_hash", limit=240, required=False)
        if not isinstance(self.canonical_mutation, bool):
            raise StructureProjectionError("canonical_mutation은 boolean이어야 합니다.")
        if not isinstance(self.filesystem_mutation, bool):
            raise StructureProjectionError("filesystem_mutation은 boolean이어야 합니다.")
        if self.canonical_mutation or self.filesystem_mutation:
            raise StructureProjectionError("StructureProjection은 상태나 파일을 변경할 수 없습니다.")

        nodes = tuple(
            sorted(
                _typed_items(self.nodes, "nodes", StructureProjectionNode, _MAX_NODES),
                key=lambda node: node.id,
            )
        )
        edges = tuple(
            sorted(
                _typed_items(self.edges, "edges", StructureProjectionEdge, _MAX_EDGES),
                key=lambda edge: edge.key,
            )
        )
        issues = tuple(
            sorted(
                _typed_items(self.issues, "issues", StructureProjectionIssue, _MAX_ISSUES)
            )
        )
        unresolved = _strings(self.unresolved, "unresolved")
        metadata = _freeze_mapping(self.metadata, "metadata")
        if len(nodes) != len({node.id for node in nodes}):
            raise StructureProjectionError("projection node id가 중복됩니다.")
        if len(edges) != len({edge.key for edge in edges}):
            raise StructureProjectionError("projection edge가 중복됩니다.")

        node_ids = {node.id for node in nodes}
        missing_endpoints = {
            endpoint
            for edge in edges
            for endpoint in (edge.source_id, edge.target_id)
            if endpoint not in node_ids
        }
        issue_subjects = {
            value
            for issue in issues
            for value in (issue.subject_id, issue.target_id)
            if value
        }
        explicit_unresolved = set(unresolved)
        if any(
            endpoint not in issue_subjects
            and f"missing_node:{endpoint}" not in explicit_unresolved
            for endpoint in missing_endpoints
        ):
            raise StructureProjectionError(
                "projection missing endpoint는 issue 또는 unresolved에 명시해야 합니다."
            )

        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "id", projection_id)
        object.__setattr__(self, "projection_hash", projection_hash)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "unresolved", unresolved)
        object.__setattr__(self, "metadata", metadata)

        computed = canonical_hash(self._hash_payload())
        if projection_hash and projection_hash != computed:
            raise StructureProjectionError("projection_hash가 payload와 일치하지 않습니다.")
        expected_id = f"structure-projection:{computed.removeprefix('sha256:')}"
        if projection_id and projection_id != expected_id:
            raise StructureProjectionError("StructureProjection id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "projection_hash", computed)
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "issues": [issue.to_record() for issue in self.issues],
            "unresolved": list(self.unresolved),
        }
        if self.metadata:
            payload["metadata"] = _thaw_json(self.metadata)
        return payload

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "issues": [issue.to_record() for issue in self.issues],
            "unresolved": list(self.unresolved),
            "projection_hash": self.projection_hash,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
        if self.metadata:
            record["metadata"] = _thaw_json(self.metadata)
        return record

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return a stable JSON rendering without writing it anywhere."""

        options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
        if indent is None:
            options["separators"] = (",", ":")
        else:
            options["indent"] = indent
        return json.dumps(self.to_record(), **options)

    def to_mermaid(self) -> str:
        """Return a syntax-safe deterministic Mermaid flowchart."""

        declared_ids = {node.id for node in self.nodes}
        all_ids = set(declared_ids)
        all_ids.update(endpoint for edge in self.edges for endpoint in (edge.source_id, edge.target_id))
        all_ids.update(
            value
            for issue in self.issues
            for value in (issue.subject_id, issue.target_id)
            if value
        )
        aliases = {node_id: f"n{index}" for index, node_id in enumerate(sorted(all_ids))}
        lines = ["flowchart TD"]

        for node in self.nodes:
            label = node.label if not node.status else f"{node.label} ({node.status})"
            lines.append(f'    {aliases[node.id]}["{_mermaid_text(label)}"]')
        for node_id in sorted(all_ids - declared_ids):
            lines.append(
                f'    {aliases[node_id]}["{_mermaid_text(f"Unresolved: {node_id}")}"]:::unresolved'
            )
        for edge in self.edges:
            relation = _mermaid_text(edge.relation)
            lines.append(f"    {aliases[edge.source_id]} -->|{relation}| {aliases[edge.target_id]}")

        for index, issue in enumerate(self.issues):
            issue_alias = f"issue{index}"
            issue_label = f"[{issue.code}] {issue.detail}"
            lines.append(f'    {issue_alias}["{_mermaid_text(issue_label)}"]:::issue')
            linked = []
            for subject_id in (issue.subject_id, issue.target_id):
                if subject_id and subject_id in aliases and aliases[subject_id] not in linked:
                    linked.append(aliases[subject_id])
            for alias in linked:
                lines.append(f"    {alias} -.-> {issue_alias}")

        lines.append("    classDef unresolved stroke:#d97706,stroke-dasharray: 5 5;")
        lines.append("    classDef issue stroke:#dc2626,stroke-dasharray: 3 3;")
        return "\n".join(lines)


def _mermaid_text(value: str) -> str:
    """Escape label text so user/model content cannot become Mermaid syntax."""

    escaped = value.replace("\r", " ").replace("\n", " ")
    return escaped.translate(
        {
            ord("&"): "&amp;",
            ord('"'): "&quot;",
            ord("<"): "&lt;",
            ord(">"): "&gt;",
            ord("|"): "&#124;",
            ord("["): "&#91;",
            ord("]"): "&#93;",
            ord("("): "&#40;",
            ord(")"): "&#41;",
            ord("{"): "&#123;",
            ord("}"): "&#125;",
            ord(";"): "&#59;",
            ord("`"): "&#96;",
            ord("\\"): "&#92;",
        }
    )


def _make_projection(
    *,
    source_kind: str,
    source_id: str,
    source_hash: str,
    nodes: Sequence[StructureProjectionNode],
    edges: Sequence[StructureProjectionEdge],
    issues: Sequence[StructureProjectionIssue] = (),
    unresolved: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> StructureProjection:
    return StructureProjection(
        source_kind=source_kind,
        source_id=source_id,
        source_hash=source_hash,
        nodes=tuple(nodes),
        edges=tuple(edges),
        issues=tuple(issues),
        unresolved=tuple(unresolved),
        metadata=metadata or {},
    )


def project_structural_observation(observation: StructuralObservation) -> StructureProjection:
    """Project immutable sensor evidence without promoting or mutating it."""

    if not isinstance(observation, StructuralObservation):
        raise StructureProjectionError("StructuralObservation만 projection할 수 있습니다.")

    nodes = [
        StructureProjectionNode(
            id=node.id,
            kind=node.kind,
            label=node.label,
            attributes={
                "locator": node.locator,
                "source_attributes": node.attributes,
                "evidence_refs": list(node.evidence_refs),
            },
        )
        for node in observation.nodes
    ]
    edges = [
        StructureProjectionEdge(
            source_id=edge.source_id,
            target_id=edge.target_id,
            relation=edge.relation,
            attributes={
                "source_attributes": edge.attributes,
                "evidence_refs": list(edge.evidence_refs),
            },
        )
        for edge in observation.edges
    ]
    issues = [
        StructureProjectionIssue(
            code=issue.code,
            detail=issue.detail,
            subject_id=issue.subject_id,
            target_id=issue.target_id,
            severity=issue.severity,
            evidence_refs=issue.evidence_refs,
        )
        for issue in observation.issues
    ]
    metadata = {
        "observation_id": observation.id,
        "observation_source_id": observation.source_id,
        "sensor_type": observation.sensor_type,
        "sensor_version": observation.sensor_version,
        "workspace_hash": observation.workspace_hash,
        "captured_at": observation.captured_at,
        "scope": observation.scope.to_record(),
        "evidence": [item.to_record() for item in observation.evidence],
    }
    return _make_projection(
        source_kind="structural_observation",
        source_id=observation.id,
        source_hash=observation.observation_hash,
        nodes=nodes,
        edges=edges,
        issues=issues,
        unresolved=observation.unresolved,
        metadata=metadata,
    )


def project_dependency_graph(graph: DependencyGraph) -> StructureProjection:
    """Project a deterministic DependencyGraph without changing it."""

    if not isinstance(graph, DependencyGraph):
        raise StructureProjectionError("DependencyGraph만 projection할 수 있습니다.")
    graph_nodes = tuple(graph.nodes)
    graph_edges = tuple(graph.edges)
    graph_issues = tuple(graph.issues)
    if any(not isinstance(node_id, str) for node_id in graph_nodes):
        raise StructureProjectionError("DependencyGraph node id가 문자열이 아닙니다.")
    if any(not isinstance(edge, GraphEdge) for edge in graph_edges):
        raise StructureProjectionError("DependencyGraph edge 타입이 잘못되었습니다.")
    if any(not isinstance(issue, GraphIssue) for issue in graph_issues):
        raise StructureProjectionError("DependencyGraph issue 타입이 잘못되었습니다.")
    provenance = {}
    for node_id in sorted(graph_nodes):
        sources = tuple(graph.provenance_sources(node_id))
        if any(not isinstance(source_id, str) for source_id in sources):
            raise StructureProjectionError("DependencyGraph provenance source가 문자열이 아닙니다.")
        provenance[node_id] = sorted(sources)

    source_payload = {
        "nodes": sorted(graph_nodes),
        "edges": [
            {
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "relation": edge.relation,
            }
            for edge in sorted(graph_edges)
        ],
        "issues": [
            {
                "source_id": issue.source_id,
                "target_id": issue.target_id,
                "relation": issue.relation,
                "kind": issue.kind,
            }
            for issue in sorted(graph_issues)
        ],
        "provenance": provenance,
    }
    source_hash = canonical_hash(source_payload)
    source_id = f"dependency-graph:{source_hash.removeprefix('sha256:')}"
    nodes = tuple(
        StructureProjectionNode(
            id=node_id,
            kind="state_item",
            label=node_id,
            attributes={"provenance_sources": provenance.get(node_id, [])},
        )
        for node_id in sorted(graph_nodes)
    )
    edges = tuple(
        StructureProjectionEdge(
            source_id=edge.source_id,
            target_id=edge.target_id,
            relation=edge.relation,
        )
        for edge in sorted(graph_edges)
    )
    issues = tuple(
        StructureProjectionIssue(
            code=issue.kind,
            detail=f"{issue.source_id} -> {issue.target_id} ({issue.relation})",
            subject_id=issue.source_id,
            target_id=issue.target_id,
        )
        for issue in sorted(graph_issues)
    )
    unresolved = tuple(
        f"missing_node:{endpoint}"
        for endpoint in sorted(
            {
                endpoint
                for issue in graph_issues
                for endpoint in (issue.source_id, issue.target_id)
                if endpoint not in set(graph_nodes)
            }
        )
    )
    return _make_projection(
        source_kind="dependency_graph",
        source_id=source_id,
        source_hash=source_hash,
        nodes=nodes,
        edges=edges,
        issues=issues,
        unresolved=unresolved,
    )


def project_structure_map(structure: StructureMapProposal) -> StructureProjection:
    """Project a StructureMap proposal, including its review-only WorkUnits."""

    if not isinstance(structure, StructureMapProposal):
        raise StructureProjectionError("StructureMapProposal만 projection할 수 있습니다.")
    map_nodes = _typed_items(structure.nodes, "structure.nodes", StructureNode, 100)
    map_edges = _typed_items(structure.edges, "structure.edges", StructureEdge, 200)
    work_units = _typed_items(structure.work_units, "structure.work_units", WorkUnit, 40)
    questions = _typed_items(structure.questions, "structure.questions", OpenQuestion, 40)

    nodes = [
        StructureProjectionNode(
            id=node.id,
            kind=node.kind,
            label=node.title,
            status=node.status,
            attributes={
                "purpose": node.purpose,
                "required": node.required,
                "completion_criteria": node.completion_criteria,
                "artifact_role": node.artifact_role,
            },
        )
        for node in map_nodes
    ]
    nodes.extend(
        StructureProjectionNode(
            id=work.id,
            kind="work_unit",
            label=work.title,
            status=work.status,
            attributes={
                "purpose": work.purpose,
                "prerequisites": list(work.prerequisites),
                "source_node_ids": list(work.source_node_ids),
                "artifact_roles": list(work.artifact_roles),
                "completion_criteria": work.completion_criteria,
                "downstream_impacts": list(work.downstream_impacts),
            },
        )
        for work in work_units
    )
    edges = [
        StructureProjectionEdge(
            source_id=edge.source_id,
            target_id=edge.target_id,
            relation=edge.relation,
        )
        for edge in map_edges
    ]
    edge_keys = {edge.key for edge in edges}
    for work in work_units:
        for prerequisite in work.prerequisites:
            edge = StructureProjectionEdge(
                source_id=work.id,
                target_id=prerequisite,
                relation="depends_on",
                attributes={"origin": "work_unit.prerequisite"},
            )
            if edge.key not in edge_keys:
                edges.append(edge)
                edge_keys.add(edge.key)

    unresolved = tuple(structure.unresolved)
    issues = [
        StructureProjectionIssue(code="unresolved", detail=value)
        for value in unresolved
    ]
    issues.extend(
        StructureProjectionIssue(
            code="blocking_question",
            detail=f"{question.id}: {question.question}",
        )
        for question in questions
        if question.blocking and question.status != "ANSWERED"
    )
    return _make_projection(
        source_kind="structure_map",
        source_id=structure.id,
        source_hash=structure.map_hash,
        nodes=nodes,
        edges=edges,
        issues=issues,
        unresolved=unresolved,
    )


_TRACE_STAGES = (
    ("report", "Bundle Report"),
    ("ir", "Synapse IR"),
    ("frame", "Cognitive Frame"),
    ("failures", "Failure Report"),
    ("plan", "Guided Build Plan"),
    ("event", "Router Event"),
    ("cards", "Card Route"),
    ("table", "Cognitive Table"),
    ("action", "Action Route"),
    ("runtime", "Runtime Plan"),
)
_TRACE_STAGE_EDGES = (
    ("report", "ir"),
    ("ir", "frame"),
    ("frame", "failures"),
    ("frame", "plan"),
    ("report", "event"),
    ("event", "cards"),
    ("cards", "table"),
    ("table", "action"),
    ("action", "runtime"),
)


def _trace_stage_status(trace: ReactiveTrace, slug: str) -> str:
    if slug == "failures":
        return "BLOCKED" if trace.failures.has_blockers else "REVIEW" if trace.failures.clusters else "READY"
    if slug == "plan":
        return trace.plan.status
    if slug == "table":
        return "REVIEW" if trace.table.unresolved or trace.table.conflicts else "READY"
    if slug == "action":
        return trace.action.status
    if slug == "runtime":
        return trace.runtime.status
    return "OBSERVED"


def _trace_stage_attributes(trace: ReactiveTrace, slug: str) -> dict[str, Any]:
    if slug == "report":
        return {"source_id": trace.report.source_id, "entry_count": trace.report.entry_count}
    if slug == "ir":
        return {"version": trace.ir.version, "node_count": len(trace.ir.nodes), "relation_count": len(trace.ir.relations)}
    if slug == "frame":
        return {"id": trace.frame.id}
    if slug == "failures":
        return {"cluster_count": len(trace.failures.clusters)}
    if slug == "plan":
        return {"id": trace.plan.id}
    if slug == "event":
        return {"id": trace.event.id}
    if slug == "cards":
        return {"selected_ids": list(trace.cards.selected_ids)}
    if slug == "table":
        return {"id": trace.table.id}
    if slug == "action":
        return {"id": trace.action.request.id}
    if slug == "runtime":
        return {"id": trace.runtime.id, "provider": trace.runtime.provider}
    return {}


def project_reactive_trace(trace: ReactiveTrace) -> StructureProjection:
    """Project the existing read-only reactive pipeline and IR relationships."""

    if not isinstance(trace, ReactiveTrace):
        raise StructureProjectionError("ReactiveTrace만 projection할 수 있습니다.")
    if not isinstance(trace.ir, SynapseIR):
        raise StructureProjectionError("ReactiveTrace.ir 타입이 잘못되었습니다.")
    ir_nodes = _typed_items(trace.ir.nodes, "trace.ir.nodes", IRNode, 100_000)
    ir_relations = _typed_items(trace.ir.relations, "trace.ir.relations", IRRelation, 200_000)
    ir_issues = trace.ir.validate(strict=False)
    if any(not isinstance(issue, IRIssue) for issue in ir_issues):
        raise StructureProjectionError("ReactiveTrace IR issue 타입이 잘못되었습니다.")

    source_record = trace.to_record()
    source_hash = canonical_hash(source_record)
    source_id = f"reactive-trace:{source_hash.removeprefix('sha256:')}"
    nodes = [
        StructureProjectionNode(
            id=f"ir:{node.id}",
            kind=f"ir:{node.kind}",
            label=node.label,
            status=node.status.value,
            attributes={
                "owner": node.owner,
                "provenance_sources": sorted({entry.source_id for entry in node.provenance}),
            },
        )
        for node in ir_nodes
    ]
    nodes.extend(
        StructureProjectionNode(
            id=f"trace-stage:{slug}",
            kind="pipeline_stage",
            label=label,
            status=_trace_stage_status(trace, slug),
            attributes=_trace_stage_attributes(trace, slug),
        )
        for slug, label in _TRACE_STAGES
    )
    edges = [
        StructureProjectionEdge(
            source_id=f"ir:{relation.source_id}",
            target_id=f"ir:{relation.target_id}",
            relation=relation.relation,
        )
        for relation in ir_relations
    ]
    edges.extend(
        StructureProjectionEdge(
            source_id=f"trace-stage:{source_slug}",
            target_id=f"trace-stage:{target_slug}",
            relation="produces",
        )
        for source_slug, target_slug in _TRACE_STAGE_EDGES
    )

    relation_by_id = {relation.id: relation for relation in ir_relations}
    issues: list[StructureProjectionIssue] = []
    unresolved: set[str] = set()
    for issue in ir_issues:
        relation = relation_by_id.get(issue.subject_id)
        subject_id = f"ir:{relation.source_id}" if relation else f"ir:{issue.subject_id}"
        target_id = f"ir:{relation.target_id}" if relation else ""
        issues.append(
            StructureProjectionIssue(
                code=issue.code,
                detail=issue.detail,
                subject_id=subject_id,
                target_id=target_id,
            )
        )
        if target_id and target_id not in {node.id for node in nodes}:
            unresolved.add(f"missing_node:{target_id}")
    for cluster in trace.failures.clusters:
        subject_id = f"ir:{cluster.subject_ids[0]}" if cluster.subject_ids else ""
        issues.append(
            StructureProjectionIssue(
                code=cluster.key,
                detail=cluster.recommendation,
                subject_id=subject_id,
            )
        )
    for value in trace.table.unresolved:
        issues.append(StructureProjectionIssue(code="table_unresolved", detail=value))
        unresolved.add(value)
    for card_id in trace.cards.unknown_cards:
        issues.append(StructureProjectionIssue(code="unknown_card", detail=card_id))
        unresolved.add(f"unknown_card:{card_id}")

    return _make_projection(
        source_kind="reactive_trace",
        source_id=source_id,
        source_hash=source_hash,
        nodes=nodes,
        edges=edges,
        issues=issues,
        unresolved=tuple(sorted(unresolved)),
    )


__all__ = [
    "STRUCTURE_PROJECTION_SCHEMA",
    "STRUCTURE_PROJECTION_SOURCE_KINDS",
    "StructureProjection",
    "StructureProjectionEdge",
    "StructureProjectionError",
    "StructureProjectionIssue",
    "StructureProjectionNode",
    "project_dependency_graph",
    "project_reactive_trace",
    "project_structural_observation",
    "project_structure_map",
]
