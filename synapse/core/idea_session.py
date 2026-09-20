"""Phase 28A idea/session contracts.

These records are deliberately local, immutable proposal contracts.  They do
not call a model, persist a session, write a workspace, or touch the Canonical
Registry.  The first user idea is stored as an immutable seed; every later
analysis must carry the seed hash back through the verification boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any


class IdeaSessionError(ValueError):
    """Raised when an idea/session contract is structurally invalid."""


GUIDED_STAGES: tuple[str, ...] = (
    "WELCOME",
    "IDEA_CAPTURE",
    "IDEA_EXPANSION_READY",
    "EXPANSION_REVIEW",
    "QUESTION_ROUND",
    "STRUCTURE_MAP_READY",
    "STRUCTURE_REVIEW",
    "STRUCTURE_APPROVED",
    "FIRST_WORK_UNIT",
    "WORK_UNIT_REVIEW",
    "NEXT_WORK_UNIT",
    "PROJECT_READY",
)

_PROPOSAL_STATUSES = {"PROPOSED", "UNRESOLVED", "BLOCKED", "DEPRECATED"}
_QUESTION_STATUSES = {"OPEN", "ANSWERED", "SKIPPED", "UNRESOLVED"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used by every Phase 28A hash."""

    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def canonical_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _text(value: Any, label: str, *, required: bool = True, limit: int | None = None) -> str:
    if not isinstance(value, str):
        raise IdeaSessionError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise IdeaSessionError(f"{label}은(는) 비어 있을 수 없습니다.")
    if limit is not None and len(result) > limit:
        raise IdeaSessionError(f"{label}이(가) 너무 깁니다.")
    return result


def _raw_text(value: Any, label: str, *, limit: int | None = None) -> str:
    """Validate raw user text without normalizing its bytes."""

    if not isinstance(value, str):
        raise IdeaSessionError(f"{label}는 문자열이어야 합니다.")
    if not value.strip():
        raise IdeaSessionError(f"{label}은(는) 비어 있을 수 없습니다.")
    if limit is not None and len(value) > limit:
        raise IdeaSessionError(f"{label}이(가) 너무 깁니다.")
    return value


def _text_tuple(value: Sequence[str], label: str, *, limit: int = 2000) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise IdeaSessionError(f"{label}는 문자열 배열이어야 합니다.")
    result: list[str] = []
    for item in value:
        result.append(_text(item, label, limit=limit))
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposalMetadata:
    """Common metadata required by the spec for non-Canonical proposals."""

    schema_version: str
    id: str
    canonical_key: str
    owner: str
    source: str
    provenance: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    authority: str = "proposal"
    priority: int = 0
    status: str = "PROPOSED"
    depends_on: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.schema_version, "schema_version")
        _text(self.id, "metadata.id")
        _text(self.canonical_key, "canonical_key")
        _text(self.owner, "owner")
        _text(self.source, "source")
        if self.version < 1:
            raise IdeaSessionError("metadata.version은 1 이상이어야 합니다.")
        if self.authority != "proposal":
            raise IdeaSessionError("Phase 28A 산출물의 authority는 proposal이어야 합니다.")
        if self.status not in _PROPOSAL_STATUSES:
            raise IdeaSessionError(f"지원하지 않는 proposal status입니다: {self.status}")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))
        object.__setattr__(self, "depends_on", _text_tuple(self.depends_on, "depends_on"))
        object.__setattr__(self, "supersedes", _text_tuple(self.supersedes, "supersedes"))
        object.__setattr__(self, "conflicts_with", _text_tuple(self.conflicts_with, "conflicts_with"))

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "canonical_key": self.canonical_key,
            "owner": self.owner,
            "source": self.source,
            "provenance": dict(self.provenance),
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "authority": self.authority,
            "priority": self.priority,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "supersedes": list(self.supersedes),
            "conflicts_with": list(self.conflicts_with),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class IdeaSeed:
    """The original user idea; raw_text can never be replaced by a model."""

    id: str
    project_name: str
    raw_text: str
    preset_hint: str = "general"
    raw_hash: str = ""
    metadata: ProposalMetadata | None = None

    def __post_init__(self) -> None:
        _text(self.id, "seed.id")
        _text(self.project_name, "project_name", limit=160)
        raw_text = _raw_text(self.raw_text, "raw_text", limit=20_000)
        preset = _text(self.preset_hint, "preset_hint", limit=64)
        expected_hash = sha256_text(raw_text)
        if self.raw_hash and self.raw_hash != expected_hash:
            raise IdeaSessionError("IdeaSeed raw_hash가 raw_text와 일치하지 않습니다.")
        object.__setattr__(self, "raw_text", raw_text)
        object.__setattr__(self, "preset_hint", preset)
        object.__setattr__(self, "raw_hash", expected_hash)
        metadata = self.metadata or ProposalMetadata(
            schema_version="guided.seed.v1",
            id=self.id,
            canonical_key=f"idea-seed:{self.id}",
            owner="user",
            source="guided-ui",
            provenance={"source_type": "user_input", "locator": self.id},
        )
        if metadata.id != self.id:
            raise IdeaSessionError("IdeaSeed metadata.id가 seed.id와 다릅니다.")
        object.__setattr__(self, "metadata", metadata)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_name": self.project_name,
            "raw_text": self.raw_text,
            "preset_hint": self.preset_hint,
            "raw_hash": self.raw_hash,
            "metadata": self.metadata.to_record() if self.metadata else None,
            "canonical_mutation": False,
        }


def create_idea_seed(
    project_name: str,
    raw_text: str,
    *,
    preset_hint: str = "general",
    owner: str = "user",
    source: str = "guided-ui",
) -> IdeaSeed:
    """Create a deterministic seed without any filesystem or model action."""

    clean_name = _text(project_name, "project_name", limit=160)
    clean_text = _raw_text(raw_text, "raw_text", limit=20_000)
    clean_preset = _text(preset_hint, "preset_hint", limit=64)
    raw_hash = sha256_text(clean_text)
    seed_id = f"seed:{hashlib.sha256(f'{clean_name}|{clean_preset}|{raw_hash}'.encode()).hexdigest()}"
    metadata = ProposalMetadata(
        schema_version="guided.seed.v1",
        id=seed_id,
        canonical_key=f"idea-seed:{seed_id}",
        owner=_text(owner, "owner"),
        source=_text(source, "source"),
        provenance={"source_type": "user_input", "locator": seed_id},
    )
    return IdeaSeed(
        id=seed_id,
        project_name=clean_name,
        raw_text=clean_text,
        preset_hint=clean_preset,
        raw_hash=raw_hash,
        metadata=metadata,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenQuestion:
    """A model/user question whose blocking impact remains explicit."""

    id: str
    question: str
    why_needed: str
    affected_nodes: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    blocking: bool = False
    answer: str | None = None
    status: str = "OPEN"

    def __post_init__(self) -> None:
        _text(self.id, "question.id")
        _text(self.question, "question")
        _text(self.why_needed, "question.why_needed")
        object.__setattr__(self, "affected_nodes", _text_tuple(self.affected_nodes, "affected_nodes", limit=160))
        object.__setattr__(self, "suggestions", _text_tuple(self.suggestions, "suggestions"))
        if self.answer is not None:
            object.__setattr__(self, "answer", _text(self.answer, "question.answer"))
        if self.status not in _QUESTION_STATUSES:
            raise IdeaSessionError(f"지원하지 않는 question status입니다: {self.status}")
        if self.status == "ANSWERED" and not self.answer:
            raise IdeaSessionError("ANSWERED 질문에는 answer가 필요합니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "why_needed": self.why_needed,
            "affected_nodes": list(self.affected_nodes),
            "suggestions": list(self.suggestions),
            "blocking": self.blocking,
            "answer": self.answer,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class UserAnswer:
    """An explicit user answer, kept separate from model-generated content."""

    question_id: str
    answer: str
    source: str = "user"
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _text(self.question_id, "answer.question_id")
        _text(self.answer, "answer.answer", limit=4_000)
        _text(self.source, "answer.source")

    def to_record(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "answer": self.answer,
            "source": self.source,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedProjectSession:
    """Serializable browser draft; it is never a Canonical authority."""

    session_id: str
    seed: IdeaSeed
    stage: str = "WELCOME"
    answers: tuple[UserAnswer, ...] = ()
    expansion: Any | None = None
    structure: Any | None = None
    approved_structure_id: str | None = None
    work_unit_id: str | None = None
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _text(self.session_id, "session_id")
        if self.stage not in GUIDED_STAGES:
            raise IdeaSessionError(f"지원하지 않는 Guided stage입니다: {self.stage}")
        answers = tuple(self.answers)
        if any(not isinstance(answer, UserAnswer) for answer in answers):
            raise IdeaSessionError("session.answers는 UserAnswer 배열이어야 합니다.")
        question_ids = [answer.question_id for answer in answers]
        if len(question_ids) != len(set(question_ids)):
            raise IdeaSessionError("한 session에서 같은 질문에 두 답을 저장할 수 없습니다.")
        object.__setattr__(self, "answers", answers)

    @property
    def answer_hash(self) -> str:
        return canonical_hash([answer.to_record() for answer in self.answers])

    def to_record(self) -> dict[str, Any]:
        def record(value: Any) -> Any:
            if value is None:
                return None
            if hasattr(value, "to_record"):
                return value.to_record()
            return _jsonable(value)

        return {
            "session_id": self.session_id,
            "seed": self.seed.to_record(),
            "stage": self.stage,
            "answers": [answer.to_record() for answer in self.answers],
            "answer_hash": self.answer_hash,
            "expansion": record(self.expansion),
            "structure": record(self.structure),
            "approved_structure_id": self.approved_structure_id,
            "work_unit_id": self.work_unit_id,
            "updated_at": self.updated_at,
            "canonical_mutation": False,
        }


def create_guided_session(seed: IdeaSeed, *, session_id: str | None = None) -> GuidedProjectSession:
    """Create a browser draft without advancing beyond the welcome stage."""

    identifier = session_id or f"guided:{hashlib.sha256(seed.id.encode('utf-8')).hexdigest()}"
    return GuidedProjectSession(session_id=identifier, seed=seed)


__all__ = [
    "GUIDED_STAGES",
    "GuidedProjectSession",
    "IdeaSeed",
    "IdeaSessionError",
    "OpenQuestion",
    "ProposalMetadata",
    "UserAnswer",
    "canonical_hash",
    "canonical_json",
    "create_guided_session",
    "create_idea_seed",
    "sha256_text",
    "utc_now",
]
