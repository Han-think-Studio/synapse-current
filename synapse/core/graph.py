"""Deterministic provenance and dependency graph for Canonical State.

The graph is derived from immutable state items. It never changes Canonical
State and it keeps missing references explicit as issues instead of silently
dropping them. This is the Phase 4 foundation used later by reviewed bundle
intake and Cognitive Table evidence selection.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from synapse.core.contracts import CANONICAL_RELATIONS, StateItem


class GraphError(ValueError):
    """Raised when a dependency graph cannot be built safely."""


def _validate_graph_relation(relation: str) -> None:
    # The graph is derived from canonical state items, so an edge or an issue may
    # only carry a relation the canonical tier admits. build_dependency_graph
    # emits exactly depends_on / conflicts_with / supersedes off StateItem fields;
    # this rejects anything else built by hand.
    if relation not in CANONICAL_RELATIONS:
        raise GraphError(
            f"graph relation은 {sorted(CANONICAL_RELATIONS)} 중 하나여야 합니다: {relation!r}"
        )


@dataclass(frozen=True, slots=True, order=True)
class GraphEdge:
    source_id: str
    target_id: str
    relation: str

    def __post_init__(self) -> None:
        _validate_graph_relation(self.relation)


@dataclass(frozen=True, slots=True, order=True)
class GraphIssue:
    source_id: str
    target_id: str
    relation: str
    kind: str = "missing_target"

    def __post_init__(self) -> None:
        _validate_graph_relation(self.relation)


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    nodes: tuple[str, ...]
    edges: tuple[GraphEdge, ...]
    issues: tuple[GraphIssue, ...]
    provenance: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(
            self,
            "provenance",
            MappingProxyType({str(key): tuple(value) for key, value in self.provenance.items()}),
        )

    def outgoing(self, node_id: str, *, relation: str | None = None) -> tuple[GraphEdge, ...]:
        return tuple(
            edge
            for edge in self.edges
            if edge.source_id == node_id and (relation is None or edge.relation == relation)
        )

    def incoming(self, node_id: str, *, relation: str | None = None) -> tuple[GraphEdge, ...]:
        return tuple(
            edge
            for edge in self.edges
            if edge.target_id == node_id and (relation is None or edge.relation == relation)
        )

    def provenance_sources(self, node_id: str) -> tuple[str, ...]:
        return self.provenance.get(node_id, ())

    def transitive_dependents(
        self,
        node_id: str,
        *,
        relations: tuple[str, ...] = ("depends_on", "conflicts_with"),
    ) -> tuple[str, ...]:
        """Return reverse-reachable dependents in deterministic BFS order."""
        if node_id not in self.nodes:
            raise GraphError(f"그래프에 없는 node입니다: {node_id}")
        allowed = set(relations)
        reverse: dict[str, list[str]] = {}
        for edge in self.edges:
            if edge.relation in allowed:
                reverse.setdefault(edge.target_id, []).append(edge.source_id)
        visited = {node_id}
        queue: deque[str] = deque(sorted(reverse.get(node_id, ())))
        result: list[str] = []
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            result.append(current)
            queue.extend(sorted(reverse.get(current, ())))
        return tuple(result)

    def require_resolved(self) -> None:
        if self.issues:
            details = ", ".join(
                f"{issue.source_id}->{issue.target_id} ({issue.relation})" for issue in self.issues
            )
            raise GraphError(f"해결되지 않은 graph reference가 있습니다: {details}")


def build_dependency_graph(items: Iterable[StateItem], *, strict: bool = False) -> DependencyGraph:
    """Build a sorted graph from state relationships and provenance links."""
    materialized = tuple(items)
    ids = [item.id for item in materialized]
    if len(set(ids)) != len(ids):
        raise GraphError("Dependency graph에 중복 node id가 있습니다.")
    nodes = tuple(sorted(ids))
    node_set = set(nodes)
    edges: set[GraphEdge] = set()
    issues: set[GraphIssue] = set()
    provenance: dict[str, tuple[str, ...]] = {}
    for item in materialized:
        provenance[item.id] = tuple(sorted({entry.source_id for entry in item.provenance}))
        relationships = (
            ("depends_on", item.depends_on),
            ("conflicts_with", item.conflicts_with),
            ("supersedes", item.supersedes),
        )
        for relation, targets in relationships:
            for target_id in targets:
                edge = GraphEdge(source_id=item.id, target_id=target_id, relation=relation)
                edges.add(edge)
                if target_id not in node_set:
                    issues.add(GraphIssue(source_id=item.id, target_id=target_id, relation=relation))
    graph = DependencyGraph(
        nodes=nodes,
        edges=tuple(sorted(edges)),
        issues=tuple(sorted(issues)),
        provenance=provenance,
    )
    if strict:
        graph.require_resolved()
    return graph


__all__ = ["DependencyGraph", "GraphEdge", "GraphError", "GraphIssue", "build_dependency_graph"]
