"""Language- and domain-neutral Synapse IR candidates.

IR is an intake representation, not Canonical State. Nodes and relations keep
owner, lifecycle, and provenance so a later Proposal bridge can validate them
without allowing a parser, ZIP, or model to write the Registry directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.contracts import LifecycleStatus, Provenance


class IRError(ValueError):
    """Raised when an IR candidate is structurally invalid."""


#: The IR intake vocabulary. IR is a normalized intermediate, not Canonical
#: State, so its relations are its own -- deliberately NOT the canonical link
#: set and NOT the physical-code axis. `contains` is the only value production
#: emits today (`bundle_report_to_ir`: a bundle contains an entry); `depends_on`
#: and `supports` are the intake-level dependency and evidence relations already
#: exercised by the IR tests. Extend this set by an accepted decision, never by
#: passing a free string through. Until this was sealed the IR relation accepted
#: any string.
IR_RELATIONS = frozenset({"contains", "depends_on", "supports"})


@dataclass(frozen=True, slots=True, order=True)
class IRIssue:
    code: str
    subject_id: str
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class IRNode:
    id: str
    kind: str
    label: str
    owner: str
    status: LifecycleStatus = LifecycleStatus.PROPOSED
    provenance: tuple[Provenance, ...] = ()
    attributes: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.kind.strip() or not self.label.strip() or not self.owner.strip():
            raise IRError("IRNode에는 id, kind, label, owner가 필요합니다.")
        if self.status is LifecycleStatus.CONFIRMED and not self.provenance:
            raise IRError("CONFIRMED IRNode에는 provenance가 필요합니다.")
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True, kw_only=True)
class IRRelation:
    id: str
    source_id: str
    target_id: str
    relation: str
    owner: str
    status: LifecycleStatus = LifecycleStatus.PROPOSED
    provenance: tuple[Provenance, ...] = ()

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.id, self.source_id, self.target_id, self.relation, self.owner)):
            raise IRError("IRRelation에는 id, source_id, target_id, relation, owner가 필요합니다.")
        if self.relation not in IR_RELATIONS:
            raise IRError(
                f"IRRelation.relation은 {sorted(IR_RELATIONS)} 중 하나여야 합니다: {self.relation!r}"
            )
        if self.source_id == self.target_id:
            raise IRError("IRRelation은 self relation을 만들 수 없습니다.")
        if self.status is LifecycleStatus.CONFIRMED and not self.provenance:
            raise IRError("CONFIRMED IRRelation에는 provenance가 필요합니다.")
        object.__setattr__(self, "provenance", tuple(self.provenance))


@dataclass(frozen=True, slots=True)
class SynapseIR:
    version: int
    nodes: tuple[IRNode, ...]
    relations: tuple[IRRelation, ...]
    metadata: Mapping[str, Any]
    issues: tuple[IRIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.version < 1:
            raise IRError("IR version은 1 이상이어야 합니다.")
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=lambda node: node.id)))
        object.__setattr__(self, "relations", tuple(sorted(self.relations, key=lambda relation: relation.id)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "issues", tuple(sorted(self.issues)))

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    def validate(self, *, strict: bool = False) -> tuple[IRIssue, ...]:
        node_ids = set(self.node_ids)
        issues = list(self.issues)
        if len(node_ids) != len(self.nodes):
            issues.append(IRIssue("duplicate_node", "ir", "IRNode id가 중복됩니다."))
        relation_ids = [relation.id for relation in self.relations]
        if len(set(relation_ids)) != len(relation_ids):
            issues.append(IRIssue("duplicate_relation", "ir", "IRRelation id가 중복됩니다."))
        for relation in self.relations:
            if relation.source_id not in node_ids or relation.target_id not in node_ids:
                issues.append(
                    IRIssue(
                        "missing_endpoint",
                        relation.id,
                        f"관계 endpoint가 없습니다: {relation.source_id}->{relation.target_id}",
                    )
                )
        result = tuple(sorted(set(issues)))
        if strict and result:
            details = ", ".join(f"{issue.code}:{issue.subject_id}" for issue in result)
            raise IRError(f"IR validation 실패: {details}")
        return result

    def to_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "nodes": [
                {
                    "id": node.id,
                    "kind": node.kind,
                    "label": node.label,
                    "owner": node.owner,
                    "status": node.status.value,
                    "provenance": [
                        {
                            "source_id": entry.source_id,
                            "method": entry.method,
                            "id": entry.id,
                            "locator": entry.locator,
                            "excerpt": entry.excerpt,
                            "captured_at": entry.captured_at,
                            "confidence": entry.confidence,
                        }
                        for entry in node.provenance
                    ],
                    "attributes": dict(node.attributes),
                }
                for node in self.nodes
            ],
            "relations": [
                {
                    "id": relation.id,
                    "source_id": relation.source_id,
                    "target_id": relation.target_id,
                    "relation": relation.relation,
                    "owner": relation.owner,
                    "status": relation.status.value,
                    "provenance": [
                        {
                            "source_id": entry.source_id,
                            "method": entry.method,
                            "id": entry.id,
                            "locator": entry.locator,
                            "excerpt": entry.excerpt,
                            "captured_at": entry.captured_at,
                            "confidence": entry.confidence,
                        }
                        for entry in relation.provenance
                    ],
                }
                for relation in self.relations
            ],
            "metadata": dict(self.metadata),
            "issues": [
                {"code": issue.code, "subject_id": issue.subject_id, "detail": issue.detail}
                for issue in self.issues
            ],
        }


def build_ir(
    nodes: Iterable[IRNode],
    relations: Iterable[IRRelation] = (),
    *,
    metadata: Mapping[str, Any] | None = None,
    strict: bool = False,
) -> SynapseIR:
    """Build and validate one immutable IR candidate document."""
    ir = SynapseIR(
        version=1,
        nodes=tuple(nodes),
        relations=tuple(relations),
        metadata=dict(metadata or {}),
    )
    ir.validate(strict=strict)
    return ir


__all__ = ["IR_RELATIONS", "IRError", "IRIssue", "IRNode", "IRRelation", "SynapseIR", "build_ir"]
