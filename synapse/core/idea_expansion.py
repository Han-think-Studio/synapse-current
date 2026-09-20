"""Strict, LLM-independent parsing for Phase 28A idea expansion proposals."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.idea_session import OpenQuestion, ProposalMetadata, canonical_hash, canonical_json


class IdeaExpansionError(ValueError):
    """Raised when an idea expansion response violates its strict contract."""


_SCHEMA_VERSIONS = {"guided.analysis.v1", "guided.session.v2"}
_ROOT_KEYS = {
    "schema_version",
    "seed_hash",
    "summary",
    "idea_expansion",
    "questions",
    "unresolved",
    "next_action",
    "structure",
}
_EXPANSION_KEYS = {
    "intended_outcome",
    "audience",
    "scope_in",
    "scope_out",
    "constraints",
    "success_criteria",
    "concepts",
    "workflows",
    "assumptions",
}
_QUESTION_KEYS = {
    "id",
    "question",
    "why_needed",
    "affected_nodes",
    "suggestions",
    "blocking",
}


def _text(value: Any, label: str, *, required: bool = True, limit: int = 4_000) -> str:
    if not isinstance(value, str):
        raise IdeaExpansionError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise IdeaExpansionError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise IdeaExpansionError(f"{label}이(가) 너무 깁니다.")
    return result


def _string_array(value: Any, label: str, *, item_limit: int = 2_000, max_items: int = 40) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise IdeaExpansionError(f"{label}는 문자열 배열이어야 합니다.")
    if len(value) > max_items:
        raise IdeaExpansionError(f"{label} 항목이 너무 많습니다.")
    result: list[str] = []
    for item in value:
        result.append(_text(item, label, limit=item_limit))
    return tuple(dict.fromkeys(result))


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IdeaExpansionError(f"{label}는 JSON 객체여야 합니다.")
    return value


def _strict_keys(value: Mapping[str, Any], allowed: set[str], label: str, required: set[str] | None = None) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise IdeaExpansionError(f"{label}에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    missing = (required or set()) - set(value)
    if missing:
        raise IdeaExpansionError(f"{label}에 필수 필드가 없습니다: {sorted(missing)}")


def _json_object(text: str) -> Mapping[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise IdeaExpansionError("모델 응답이 비어 있습니다.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IdeaExpansionError("모델 응답은 JSON 객체만 허용됩니다.") from exc
    if not isinstance(payload, Mapping):
        raise IdeaExpansionError("모델 응답 최상위 값은 JSON 객체여야 합니다.")
    return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class IdeaExpansionProposal:
    """A model-produced elaboration that never overwrites its IdeaSeed."""

    metadata: ProposalMetadata
    seed_hash: str
    revision: int
    summary: str
    intended_outcome: str
    audience: tuple[str, ...] = ()
    scope_in: tuple[str, ...] = ()
    scope_out: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    questions: tuple[OpenQuestion, ...] = ()
    unresolved: tuple[str, ...] = ()
    next_action: Mapping[str, Any] = field(default_factory=dict)
    expansion_hash: str = ""

    def __post_init__(self) -> None:
        if not self.seed_hash.strip():
            raise IdeaExpansionError("IdeaExpansion seed_hash가 필요합니다.")
        if self.revision < 1:
            raise IdeaExpansionError("IdeaExpansion revision은 1 이상이어야 합니다.")
        if self.metadata.status != "PROPOSED":
            raise IdeaExpansionError("IdeaExpansion은 PROPOSED로 시작해야 합니다.")
        if self.metadata.id != self.id:
            raise IdeaExpansionError("IdeaExpansion metadata.id가 proposal id와 다릅니다.")
        _text(self.summary, "summary")
        _text(self.intended_outcome, "intended_outcome")
        for label, value in (
            ("audience", self.audience),
            ("scope_in", self.scope_in),
            ("scope_out", self.scope_out),
            ("constraints", self.constraints),
            ("success_criteria", self.success_criteria),
            ("concepts", self.concepts),
            ("workflows", self.workflows),
            ("assumptions", self.assumptions),
            ("unresolved", self.unresolved),
        ):
            if isinstance(value, (str, bytes)):
                raise IdeaExpansionError(f"{label}는 문자열 배열이어야 합니다.")
            object.__setattr__(self, label, tuple(value))
        questions = tuple(self.questions)
        if any(not isinstance(question, OpenQuestion) for question in questions):
            raise IdeaExpansionError("questions는 OpenQuestion 배열이어야 합니다.")
        if len({question.id for question in questions}) != len(questions):
            raise IdeaExpansionError("questions ID가 중복됩니다.")
        object.__setattr__(self, "questions", questions)
        object.__setattr__(self, "next_action", MappingProxyType(dict(self.next_action)))
        computed = canonical_hash(self._hash_payload())
        if self.expansion_hash and self.expansion_hash != computed:
            raise IdeaExpansionError("IdeaExpansion expansion_hash가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "expansion_hash", computed)

    @property
    def id(self) -> str:
        return self.metadata.id

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "seed_hash": self.seed_hash,
            "revision": self.revision,
            "summary": self.summary,
            "intended_outcome": self.intended_outcome,
            "audience": list(self.audience),
            "scope_in": list(self.scope_in),
            "scope_out": list(self.scope_out),
            "constraints": list(self.constraints),
            "success_criteria": list(self.success_criteria),
            "concepts": list(self.concepts),
            "workflows": list(self.workflows),
            "assumptions": list(self.assumptions),
            "questions": [question.to_record() for question in self.questions],
            "unresolved": list(self.unresolved),
            "next_action": dict(self.next_action),
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "metadata": self.metadata.to_record(),
            "seed_hash": self.seed_hash,
            "revision": self.revision,
            "summary": self.summary,
            "idea_expansion": {
                "intended_outcome": self.intended_outcome,
                "audience": list(self.audience),
                "scope_in": list(self.scope_in),
                "scope_out": list(self.scope_out),
                "constraints": list(self.constraints),
                "success_criteria": list(self.success_criteria),
                "concepts": list(self.concepts),
                "workflows": list(self.workflows),
                "assumptions": list(self.assumptions),
            },
            "questions": [question.to_record() for question in self.questions],
            "unresolved": list(self.unresolved),
            "next_action": dict(self.next_action),
            "expansion_hash": self.expansion_hash,
            "canonical_mutation": False,
        }


def _questions(value: Any) -> tuple[OpenQuestion, ...]:
    if not isinstance(value, list):
        raise IdeaExpansionError("questions는 객체 배열이어야 합니다.")
    if len(value) > 12:
        raise IdeaExpansionError("questions 항목이 너무 많습니다.")
    result: list[OpenQuestion] = []
    for item in value:
        raw = _mapping(item, "question")
        _strict_keys(raw, _QUESTION_KEYS, "question", required=_QUESTION_KEYS)
        affected = _string_array(raw["affected_nodes"], "affected_nodes", item_limit=160)
        suggestions = _string_array(raw["suggestions"], "suggestions")
        blocking = raw["blocking"]
        if not isinstance(blocking, bool):
            raise IdeaExpansionError("question.blocking은 boolean이어야 합니다.")
        result.append(
            OpenQuestion(
                id=_text(raw["id"], "question.id", limit=160),
                question=_text(raw["question"], "question.question"),
                why_needed=_text(raw["why_needed"], "question.why_needed"),
                affected_nodes=affected,
                suggestions=suggestions,
                blocking=blocking,
            )
        )
    if len({question.id for question in result}) != len(result):
        raise IdeaExpansionError("questions ID가 중복됩니다.")
    return tuple(result)


def parse_idea_expansion_response(
    text: str,
    *,
    expected_seed_hash: str,
    revision: int = 1,
    owner: str = "human-ui",
    source: str = "guided-ui",
    provenance: Mapping[str, Any] | None = None,
) -> IdeaExpansionProposal:
    """Parse expansion-only JSON; a sibling ``structure`` is left for its parser."""

    payload = _json_object(text)
    _strict_keys(payload, _ROOT_KEYS, "expansion response", required={"schema_version", "seed_hash", "summary", "idea_expansion", "questions", "unresolved"})
    schema_version = _text(payload["schema_version"], "schema_version", limit=80)
    if schema_version not in _SCHEMA_VERSIONS:
        raise IdeaExpansionError(f"지원하지 않는 schema_version입니다: {schema_version}")
    seed_hash = _text(payload["seed_hash"], "seed_hash", limit=100)
    if seed_hash != expected_seed_hash:
        raise IdeaExpansionError("응답 seed_hash가 현재 IdeaSeed와 다릅니다.")
    expansion = _mapping(payload["idea_expansion"], "idea_expansion")
    _strict_keys(expansion, _EXPANSION_KEYS, "idea_expansion", required={"intended_outcome", "success_criteria"})
    arrays: dict[str, tuple[str, ...]] = {}
    for key in _EXPANSION_KEYS - {"intended_outcome"}:
        arrays[key] = _string_array(expansion.get(key, []), f"idea_expansion.{key}")
    unresolved = _string_array(payload["unresolved"], "unresolved")
    next_action = payload.get("next_action", {})
    if not isinstance(next_action, Mapping):
        raise IdeaExpansionError("next_action은 JSON 객체여야 합니다.")
    questions = _questions(payload["questions"])
    summary = _text(payload["summary"], "summary")
    intended_outcome = _text(expansion["intended_outcome"], "idea_expansion.intended_outcome")
    canonical = canonical_json(
        {
            "seed_hash": seed_hash,
            "revision": revision,
            "summary": summary,
            "idea_expansion": dict(expansion),
            "questions": [question.to_record() for question in questions],
            "unresolved": list(unresolved),
            "next_action": dict(next_action),
        }
    )
    proposal_id = f"expansion:{canonical_hash(canonical)}"
    # Parsing the same model response must be reproducible.  Proposal metadata
    # is still PROPOSED and memory-only; its audit markers are derived from the
    # immutable proposal id instead of wall-clock time so repeated verification
    # cannot produce different records.
    deterministic_timestamp = f"deterministic:{proposal_id}"
    metadata = ProposalMetadata(
        schema_version=schema_version,
        id=proposal_id,
        canonical_key=f"idea-expansion:{proposal_id}",
        owner=_text(owner, "owner"),
        source=_text(source, "source"),
        provenance=dict(provenance or {}),
        created_at=deterministic_timestamp,
        updated_at=deterministic_timestamp,
    )
    return IdeaExpansionProposal(
        metadata=metadata,
        seed_hash=seed_hash,
        revision=revision,
        summary=summary,
        intended_outcome=intended_outcome,
        audience=arrays.get("audience", ()),
        scope_in=arrays.get("scope_in", ()),
        scope_out=arrays.get("scope_out", ()),
        constraints=arrays.get("constraints", ()),
        success_criteria=arrays.get("success_criteria", ()),
        concepts=arrays.get("concepts", ()),
        workflows=arrays.get("workflows", ()),
        assumptions=arrays.get("assumptions", ()),
        questions=questions,
        unresolved=unresolved,
        next_action=next_action,
    )


__all__ = ["IdeaExpansionError", "IdeaExpansionProposal", "parse_idea_expansion_response"]
