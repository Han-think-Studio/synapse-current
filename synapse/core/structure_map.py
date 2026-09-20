"""Phase 28A deterministic StructureMap contracts and revision diff."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.idea_session import (
    OpenQuestion,
    ProposalMetadata,
    canonical_hash,
)


class StructureMapError(ValueError):
    """Raised when a StructureMap proposal is not deterministic and safe."""


NODE_KINDS = {"goal", "context", "entity", "relation", "rule", "work", "review", "artifact"}
NODE_STATUSES = {"known", "proposed", "unanswered", "blocked", "approved"}
EDGE_RELATIONS = {"depends_on", "informs", "constrains", "produces", "reviews"}
WORK_STATUSES = {"locked", "ready", "candidate", "approved", "applied"}

# The exact field sets a model response's node/edge/work_unit objects must
# have -- one authority shared by parse_structure_map_response's strict-key
# check below and by any response_format schema built from this module
# (see guided_analysis_candidate.build_guided_analysis_json_schema).
NODE_RESPONSE_KEYS = {"id", "kind", "title", "purpose", "required", "source_refs", "completion_criteria", "status", "artifact_role"}
EDGE_RESPONSE_KEYS = {"from", "to", "relation"}
WORK_UNIT_RESPONSE_KEYS = {"id", "title", "purpose", "prerequisites", "source_node_ids", "artifact_roles", "completion_criteria", "downstream_impacts", "status"}

# This is a logical-role allowlist, not a model-controlled path registry.
ARTIFACT_ROLE_PATHS: Mapping[str, str] = MappingProxyType(
    {
        "project_manifest": "synapse.project.yaml",
        "readme": "README.md",
        "idea": "00_intake/idea.md",
        "goals": "01_context/goals.md",
        "constraints": "01_context/constraints.md",
        "entities": "02_model/entities.md",
        "relations": "02_model/relations.md",
        "invariants": "03_rules/invariants.md",
        "next_steps": "04_work/next_steps.md",
        "open_questions": "99_review/open_questions.md",
        "subjects": "02_model/subjects.md",
        "continuity": "03_rules/continuity.md",
        "outline": "04_work/outline.md",
        "interfaces": "02_model/interfaces.md",
        "tests": "05_validation/tests.md",
        "sources": "02_model/sources.md",
        "method": "03_rules/method.md",
        "experiments": "04_work/experiments.md",
        "inputs_outputs": "02_model/inputs_outputs.md",
        "safety": "03_rules/safety.md",
        "runs": "04_work/runs.md",
        "premises": "03_rules/premises.md",
        "failure_criteria": "05_validation/failure_criteria.md",
    }
)


def _text(value: Any, label: str, *, required: bool = True, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        raise StructureMapError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise StructureMapError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise StructureMapError(f"{label}이(가) 너무 깁니다.")
    return result


def _strings(value: Sequence[str], label: str, *, max_items: int = 80, item_limit: int = 240) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise StructureMapError(f"{label}는 문자열 배열이어야 합니다.")
    if len(value) > max_items:
        raise StructureMapError(f"{label} 항목이 너무 많습니다.")
    result = tuple(_text(item, label, limit=item_limit) for item in value)
    if len(set(result)) != len(result):
        raise StructureMapError(f"{label}에 중복 항목이 있습니다.")
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructureMapError(f"{label}는 JSON 객체여야 합니다.")
    return value


def _strict_keys(value: Mapping[str, Any], allowed: set[str], label: str, required: set[str] | None = None) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise StructureMapError(f"{label}에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    missing = (required or set()) - set(value)
    if missing:
        raise StructureMapError(f"{label}에 필수 필드가 없습니다: {sorted(missing)}")


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureNode:
    id: str
    kind: str
    title: str
    purpose: str
    required: bool = False
    source_refs: tuple[str, ...] = ()
    completion_criteria: str = ""
    status: str = "proposed"
    artifact_role: str | None = None

    def __post_init__(self) -> None:
        _text(self.id, "node.id", limit=160)
        if self.kind not in NODE_KINDS:
            raise StructureMapError(f"지원하지 않는 node kind입니다: {self.kind}")
        _text(self.title, "node.title")
        _text(self.purpose, "node.purpose")
        if self.status not in NODE_STATUSES:
            raise StructureMapError(f"지원하지 않는 node status입니다: {self.status}")
        if self.required and not self.completion_criteria.strip():
            raise StructureMapError("required node에는 completion_criteria가 필요합니다.")
        if self.artifact_role is not None:
            role = _text(self.artifact_role, "node.artifact_role", limit=80)
            if role not in ARTIFACT_ROLE_PATHS:
                raise StructureMapError(f"알 수 없는 artifact_role입니다: {role}")
            object.__setattr__(self, "artifact_role", role)
        object.__setattr__(self, "source_refs", _strings(self.source_refs, "node.source_refs"))
        object.__setattr__(self, "completion_criteria", self.completion_criteria.strip())

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "purpose": self.purpose,
            "required": self.required,
            "source_refs": list(self.source_refs),
            "completion_criteria": self.completion_criteria,
            "status": self.status,
            "artifact_role": self.artifact_role,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureEdge:
    source_id: str
    target_id: str
    relation: str

    def __post_init__(self) -> None:
        _text(self.source_id, "edge.from", limit=160)
        _text(self.target_id, "edge.to", limit=160)
        if self.source_id == self.target_id:
            raise StructureMapError("edge가 자기 자신을 참조할 수 없습니다.")
        if self.relation not in EDGE_RELATIONS:
            raise StructureMapError(f"지원하지 않는 edge relation입니다: {self.relation}")

    def to_record(self) -> dict[str, str]:
        return {"from": self.source_id, "to": self.target_id, "relation": self.relation}


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkUnit:
    id: str
    title: str
    purpose: str
    prerequisites: tuple[str, ...] = ()
    source_node_ids: tuple[str, ...] = ()
    artifact_roles: tuple[str, ...] = ()
    completion_criteria: str = ""
    downstream_impacts: tuple[str, ...] = ()
    status: str = "locked"

    def __post_init__(self) -> None:
        _text(self.id, "work_unit.id", limit=160)
        _text(self.title, "work_unit.title")
        _text(self.purpose, "work_unit.purpose")
        if self.status not in WORK_STATUSES:
            raise StructureMapError(f"지원하지 않는 work unit status입니다: {self.status}")
        if not self.completion_criteria.strip():
            raise StructureMapError("WorkUnit에는 completion_criteria가 필요합니다.")
        object.__setattr__(self, "prerequisites", _strings(self.prerequisites, "work_unit.prerequisites"))
        object.__setattr__(self, "source_node_ids", _strings(self.source_node_ids, "work_unit.source_node_ids"))
        roles = _strings(self.artifact_roles, "work_unit.artifact_roles", item_limit=80)
        if any(role not in ARTIFACT_ROLE_PATHS for role in roles):
            raise StructureMapError("WorkUnit에 알 수 없는 artifact_role이 있습니다.")
        object.__setattr__(self, "artifact_roles", roles)
        object.__setattr__(self, "downstream_impacts", _strings(self.downstream_impacts, "work_unit.downstream_impacts"))
        object.__setattr__(self, "completion_criteria", self.completion_criteria.strip())

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "purpose": self.purpose,
            "prerequisites": list(self.prerequisites),
            "source_node_ids": list(self.source_node_ids),
            "artifact_roles": list(self.artifact_roles),
            "completion_criteria": self.completion_criteria,
            "downstream_impacts": list(self.downstream_impacts),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureDiff:
    base_map_hash: str | None
    current_map_hash: str
    added_node_ids: tuple[str, ...] = ()
    changed_node_ids: tuple[str, ...] = ()
    removed_node_ids: tuple[str, ...] = ()
    added_work_unit_ids: tuple[str, ...] = ()
    changed_work_unit_ids: tuple[str, ...] = ()
    removed_work_unit_ids: tuple[str, ...] = ()
    possible_moves: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "base_map_hash": self.base_map_hash,
            "current_map_hash": self.current_map_hash,
            "added_node_ids": list(self.added_node_ids),
            "changed_node_ids": list(self.changed_node_ids),
            "removed_node_ids": list(self.removed_node_ids),
            "added_work_unit_ids": list(self.added_work_unit_ids),
            "changed_work_unit_ids": list(self.changed_work_unit_ids),
            "removed_work_unit_ids": list(self.removed_work_unit_ids),
            "possible_moves": list(self.possible_moves),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureMapProposal:
    metadata: ProposalMetadata
    seed_hash: str
    expansion_id: str
    revision: int
    parent_map_hash: str | None
    preset_hint: str
    nodes: tuple[StructureNode, ...]
    edges: tuple[StructureEdge, ...]
    work_units: tuple[WorkUnit, ...]
    questions: tuple[OpenQuestion, ...] = ()
    unresolved: tuple[str, ...] = ()
    next_recommended_action: Mapping[str, Any] = field(default_factory=dict)
    map_hash: str = ""
    revision_diff: StructureDiff | None = None

    def __post_init__(self) -> None:
        if not self.seed_hash.strip() or not self.expansion_id.strip():
            raise StructureMapError("StructureMap seed_hash와 expansion_id가 필요합니다.")
        if self.revision < 1:
            raise StructureMapError("StructureMap revision은 1 이상이어야 합니다.")
        if self.metadata.status != "PROPOSED":
            raise StructureMapError("StructureMap은 PROPOSED로 시작해야 합니다.")
        if self.metadata.id != self.id:
            raise StructureMapError("StructureMap metadata.id가 proposal id와 다릅니다.")
        _text(self.preset_hint, "preset_hint", limit=64)
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        work_units = tuple(self.work_units)
        questions = tuple(self.questions)
        if not nodes:
            raise StructureMapError("StructureMap에는 node가 하나 이상 필요합니다.")
        if len(nodes) > 40 or len(edges) > 80 or len(work_units) > 12 or len(questions) > 12:
            raise StructureMapError("StructureMap 크기 제한을 초과했습니다.")
        if len({node.id for node in nodes}) != len(nodes):
            raise StructureMapError("StructureMap node ID가 중복됩니다.")
        if len({work.id for work in work_units}) != len(work_units):
            raise StructureMapError("StructureMap WorkUnit ID가 중복됩니다.")
        if len({question.id for question in questions}) != len(questions):
            raise StructureMapError("StructureMap question ID가 중복됩니다.")
        node_ids = {node.id for node in nodes}
        for edge in edges:
            if edge.source_id not in node_ids or edge.target_id not in node_ids:
                raise StructureMapError("edge가 존재하지 않는 node를 참조합니다.")
        for work in work_units:
            if any(item not in node_ids and item not in {candidate.id for candidate in work_units} for item in work.prerequisites):
                raise StructureMapError("WorkUnit prerequisite가 존재하지 않는 항목을 참조합니다.")
            if any(item not in node_ids for item in work.source_node_ids):
                raise StructureMapError("WorkUnit source_node_ids가 존재하지 않는 node를 참조합니다.")
        for question in questions:
            if any(item not in node_ids for item in question.affected_nodes):
                raise StructureMapError("question이 존재하지 않는 node를 affected_nodes로 참조합니다.")
        _ensure_acyclic(node_ids, edges)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "work_units", work_units)
        object.__setattr__(self, "questions", questions)
        object.__setattr__(self, "unresolved", tuple(self.unresolved))
        object.__setattr__(self, "next_recommended_action", MappingProxyType(dict(self.next_recommended_action)))
        computed = canonical_hash(self._hash_payload())
        if self.map_hash and self.map_hash != computed:
            raise StructureMapError("StructureMap map_hash가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "map_hash", computed)
        if self.revision_diff is not None and self.revision_diff.current_map_hash != computed:
            raise StructureMapError("revision_diff가 현재 map_hash를 가리키지 않습니다.")

    @property
    def id(self) -> str:
        return self.metadata.id

    @property
    def blocking_questions(self) -> tuple[OpenQuestion, ...]:
        return tuple(question for question in self.questions if question.blocking and question.status != "ANSWERED")

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "seed_hash": self.seed_hash,
            "expansion_id": self.expansion_id,
            "revision": self.revision,
            "parent_map_hash": self.parent_map_hash,
            "preset_hint": self.preset_hint,
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "work_units": [work.to_record() for work in self.work_units],
            "questions": [question.to_record() for question in self.questions],
            "unresolved": list(self.unresolved),
            "next_recommended_action": dict(self.next_recommended_action),
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "metadata": self.metadata.to_record(),
            "seed_hash": self.seed_hash,
            "expansion_id": self.expansion_id,
            "revision": self.revision,
            "parent_map_hash": self.parent_map_hash,
            "preset_hint": self.preset_hint,
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
            "work_units": [work.to_record() for work in self.work_units],
            "questions": [question.to_record() for question in self.questions],
            "unresolved": list(self.unresolved),
            "next_recommended_action": dict(self.next_recommended_action),
            "map_hash": self.map_hash,
            "revision_diff": self.revision_diff.to_record() if self.revision_diff else None,
            "canonical_mutation": False,
        }


def _ensure_acyclic(node_ids: set[str], edges: Sequence[StructureEdge]) -> None:
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        if edge.relation == "depends_on":
            adjacency[edge.source_id].append(edge.target_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise StructureMapError("depends_on 관계에 순환이 있습니다.")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target_id in sorted(adjacency[node_id]):
            visit(target_id)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(node_ids):
        visit(node_id)


def compute_structure_diff(previous: StructureMapProposal | None, current: StructureMapProposal) -> StructureDiff:
    """Compute a stable diff; model-provided diff data is never trusted."""

    previous_nodes = {node.id: node.to_record() for node in previous.nodes} if previous else {}
    current_nodes = {node.id: node.to_record() for node in current.nodes}
    previous_work = {work.id: work.to_record() for work in previous.work_units} if previous else {}
    current_work = {work.id: work.to_record() for work in current.work_units}
    return StructureDiff(
        base_map_hash=previous.map_hash if previous else None,
        current_map_hash=current.map_hash,
        added_node_ids=tuple(sorted(set(current_nodes) - set(previous_nodes))),
        changed_node_ids=tuple(sorted(node_id for node_id in set(current_nodes) & set(previous_nodes) if current_nodes[node_id] != previous_nodes[node_id])),
        removed_node_ids=tuple(sorted(set(previous_nodes) - set(current_nodes))),
        added_work_unit_ids=tuple(sorted(set(current_work) - set(previous_work))),
        changed_work_unit_ids=tuple(sorted(work_id for work_id in set(current_work) & set(previous_work) if current_work[work_id] != previous_work[work_id])),
        removed_work_unit_ids=tuple(sorted(set(previous_work) - set(current_work))),
        possible_moves=(),
    )


def _json_object(text: str) -> Mapping[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise StructureMapError("모델 응답이 비어 있습니다.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructureMapError("모델 응답은 JSON 객체만 허용됩니다.") from exc
    if not isinstance(payload, Mapping):
        raise StructureMapError("모델 응답 최상위 값은 JSON 객체여야 합니다.")
    return payload


def _parse_questions(value: Any) -> tuple[OpenQuestion, ...]:
    if not isinstance(value, list) or len(value) > 12:
        raise StructureMapError("questions는 최대 12개의 객체 배열이어야 합니다.")
    result: list[OpenQuestion] = []
    allowed = {"id", "question", "why_needed", "affected_nodes", "suggestions", "blocking"}
    for item in value:
        raw = _mapping(item, "question")
        unknown = set(raw) - allowed
        if unknown or set(raw) != allowed:
            raise StructureMapError("question 필드가 계약과 다릅니다.")
        affected = tuple(_text(item_id, "question.affected_nodes", limit=160) for item_id in raw["affected_nodes"])
        suggestions = tuple(_text(suggestion, "question.suggestions") for suggestion in raw["suggestions"])
        if not isinstance(raw["affected_nodes"], list) or not isinstance(raw["suggestions"], list):
            raise StructureMapError("question affected_nodes/suggestions는 배열이어야 합니다.")
        if not isinstance(raw["blocking"], bool):
            raise StructureMapError("question.blocking은 boolean이어야 합니다.")
        result.append(
            OpenQuestion(
                id=_text(raw["id"], "question.id", limit=160),
                question=_text(raw["question"], "question.question"),
                why_needed=_text(raw["why_needed"], "question.why_needed"),
                affected_nodes=affected,
                suggestions=suggestions,
                blocking=raw["blocking"],
            )
        )
    if len({question.id for question in result}) != len(result):
        raise StructureMapError("question ID가 중복됩니다.")
    return tuple(result)


def parse_structure_map_response(
    text: str,
    *,
    expected_seed_hash: str,
    expansion_id: str,
    revision: int = 1,
    parent_map_hash: str | None = None,
    preset_hint: str = "general",
    owner: str = "human-ui",
    source: str = "guided-ui",
    provenance: Mapping[str, Any] | None = None,
) -> StructureMapProposal:
    """Parse strict structure JSON; paths and model-supplied diffs are rejected."""

    payload = _json_object(text)
    allowed_root = {"schema_version", "seed_hash", "structure", "questions", "unresolved", "next_action"}
    _strict_keys(payload, allowed_root, "structure response", required={"schema_version", "seed_hash", "structure", "questions", "unresolved"})
    schema_version = _text(payload["schema_version"], "schema_version", limit=80)
    if schema_version not in {"guided.analysis.v1", "guided.session.v2"}:
        raise StructureMapError(f"지원하지 않는 schema_version입니다: {schema_version}")
    seed_hash = _text(payload["seed_hash"], "seed_hash", limit=100)
    if seed_hash != expected_seed_hash:
        raise StructureMapError("응답 seed_hash가 현재 IdeaSeed와 다릅니다.")
    structure = _mapping(payload["structure"], "structure")
    _strict_keys(structure, {"nodes", "edges", "work_units"}, "structure", required={"nodes", "edges", "work_units"})
    raw_nodes = structure["nodes"]
    raw_edges = structure["edges"]
    raw_work = structure["work_units"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise StructureMapError("structure.nodes는 하나 이상의 배열이어야 합니다.")
    if not isinstance(raw_edges, list) or not isinstance(raw_work, list):
        raise StructureMapError("structure.edges/work_units는 배열이어야 합니다.")
    if len(raw_nodes) > 40 or len(raw_edges) > 80 or len(raw_work) > 12:
        raise StructureMapError("StructureMap 크기 제한을 초과했습니다.")
    nodes: list[StructureNode] = []
    for item in raw_nodes:
        raw = _mapping(item, "node")
        _strict_keys(raw, NODE_RESPONSE_KEYS, "node", required=NODE_RESPONSE_KEYS)
        if not isinstance(raw["required"], bool):
            raise StructureMapError("node.required는 boolean이어야 합니다.")
        if not isinstance(raw["source_refs"], list):
            raise StructureMapError("node.source_refs는 배열이어야 합니다.")
        nodes.append(
            StructureNode(
                id=_text(raw["id"], "node.id", limit=160),
                kind=_text(raw["kind"], "node.kind", limit=40),
                title=_text(raw["title"], "node.title"),
                purpose=_text(raw["purpose"], "node.purpose"),
                required=raw["required"],
                source_refs=tuple(raw["source_refs"]),
                completion_criteria=_text(raw["completion_criteria"], "node.completion_criteria", required=False),
                status=_text(raw["status"], "node.status", limit=40),
                artifact_role=raw["artifact_role"],
            )
        )
    edges: list[StructureEdge] = []
    for item in raw_edges:
        raw = _mapping(item, "edge")
        _strict_keys(raw, EDGE_RESPONSE_KEYS, "edge", required=EDGE_RESPONSE_KEYS)
        edges.append(
            StructureEdge(
                source_id=_text(raw["from"], "edge.from", limit=160),
                target_id=_text(raw["to"], "edge.to", limit=160),
                relation=_text(raw["relation"], "edge.relation", limit=40),
            )
        )
    work_units: list[WorkUnit] = []
    for item in raw_work:
        raw = _mapping(item, "work_unit")
        _strict_keys(raw, WORK_UNIT_RESPONSE_KEYS, "work_unit", required=WORK_UNIT_RESPONSE_KEYS)
        for key in ("prerequisites", "source_node_ids", "artifact_roles", "downstream_impacts"):
            if not isinstance(raw[key], list):
                raise StructureMapError(f"work_unit.{key}는 배열이어야 합니다.")
        work_units.append(
            WorkUnit(
                id=_text(raw["id"], "work_unit.id", limit=160),
                title=_text(raw["title"], "work_unit.title"),
                purpose=_text(raw["purpose"], "work_unit.purpose"),
                prerequisites=tuple(raw["prerequisites"]),
                source_node_ids=tuple(raw["source_node_ids"]),
                artifact_roles=tuple(raw["artifact_roles"]),
                completion_criteria=_text(raw["completion_criteria"], "work_unit.completion_criteria"),
                downstream_impacts=tuple(raw["downstream_impacts"]),
                status=_text(raw["status"], "work_unit.status", limit=40),
            )
        )
    questions = _parse_questions(payload["questions"])
    unresolved = payload["unresolved"]
    if not isinstance(unresolved, list):
        raise StructureMapError("unresolved는 문자열 배열이어야 합니다.")
    unresolved_tuple = tuple(_text(item, "unresolved") for item in unresolved)
    next_action = payload.get("next_action", {})
    if not isinstance(next_action, Mapping):
        raise StructureMapError("next_action은 JSON 객체여야 합니다.")
    canonical_payload = {
        "seed_hash": seed_hash,
        "expansion_id": expansion_id,
        "revision": revision,
        "parent_map_hash": parent_map_hash,
        "preset_hint": preset_hint,
        "nodes": [node.to_record() for node in nodes],
        "edges": [edge.to_record() for edge in edges],
        "work_units": [work.to_record() for work in work_units],
        "questions": [question.to_record() for question in questions],
        "unresolved": list(unresolved_tuple),
        "next_recommended_action": dict(next_action),
    }
    map_hash = canonical_hash(canonical_payload)
    proposal_id = f"structure:{map_hash}"
    metadata = ProposalMetadata(
        schema_version=schema_version,
        id=proposal_id,
        canonical_key=f"structure-map:{proposal_id}",
        owner=_text(owner, "owner"),
        source=_text(source, "source"),
        provenance=dict(provenance or {}),
    )
    return StructureMapProposal(
        metadata=metadata,
        seed_hash=seed_hash,
        expansion_id=_text(expansion_id, "expansion_id", limit=200),
        revision=revision,
        parent_map_hash=parent_map_hash,
        preset_hint=_text(preset_hint, "preset_hint", limit=64),
        nodes=tuple(nodes),
        edges=tuple(edges),
        work_units=tuple(work_units),
        questions=questions,
        unresolved=unresolved_tuple,
        next_recommended_action=next_action,
        map_hash=map_hash,
    )


__all__ = [
    "ARTIFACT_ROLE_PATHS",
    "EDGE_RELATIONS",
    "EDGE_RESPONSE_KEYS",
    "NODE_KINDS",
    "NODE_RESPONSE_KEYS",
    "NODE_STATUSES",
    "WORK_STATUSES",
    "WORK_UNIT_RESPONSE_KEYS",
    "StructureDiff",
    "StructureEdge",
    "StructureMapError",
    "StructureMapProposal",
    "StructureNode",
    "WorkUnit",
    "compute_structure_diff",
    "parse_structure_map_response",
]
