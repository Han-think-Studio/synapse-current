"""Phase 1 data contracts for Synapse Core.

These contracts deliberately have no dependency on an LLM, GPU, vector
database, web framework, or cloud service. They model facts and relationships;
they do not decide whether a model proposal is true.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any

from synapse.core.errors import InvariantViolation


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LifecycleStatus(str, Enum):
    """Explicit state labels; no implicit promotion is allowed."""

    CONFIRMED = "CONFIRMED"
    ASSUMED = "ASSUMED"
    PROPOSED = "PROPOSED"
    UNRESOLVED = "UNRESOLVED"
    BLOCKED = "BLOCKED"
    DEPRECATED = "DEPRECATED"


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    """A traceable link from state to the source that produced it."""

    source_id: str
    method: str
    id: str = ""
    locator: str | None = None
    excerpt: str | None = None
    captured_at: str = field(default_factory=utc_now)
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise InvariantViolation("Provenance에는 source_id가 필요합니다.")
        if not self.method.strip():
            raise InvariantViolation("Provenance에는 method가 필요합니다.")
        if not self.id:
            object.__setattr__(self, "id", f"prov:{self.source_id}:{self.method}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise InvariantViolation("Provenance confidence는 0과 1 사이여야 합니다.")


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """An origin record used by Provenance; it is not model output."""

    id: str
    owner: str
    source_type: str
    locator: str
    title: str | None = None
    version: str | None = None
    authority: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _validate_identity(self.id, self.owner)
        if not self.source_type.strip() or not self.locator.strip():
            raise InvariantViolation("Source에는 source_type과 locator가 필요합니다.")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True, kw_only=True)
class StateItem:
    """Common metadata shared by all Canonical State records."""

    id: str
    owner: str
    canonical_key: str | None = None
    status: LifecycleStatus = LifecycleStatus.UNRESOLVED
    version: int = 1
    authority: str | None = None
    priority: int = 0
    provenance: tuple[Provenance, ...] = ()
    depends_on: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _validate_identity(self.id, self.owner)
        if self.canonical_key is None:
            object.__setattr__(self, "canonical_key", self.id)
        if self.version < 1:
            raise InvariantViolation("version은 1 이상이어야 합니다.")
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "supersedes", tuple(self.supersedes))
        object.__setattr__(self, "conflicts_with", tuple(self.conflicts_with))
        if self.status is LifecycleStatus.CONFIRMED and not self.provenance:
            raise InvariantViolation("CONFIRMED 상태는 provenance 없이 생성할 수 없습니다.")


#: The relation of record. These are exactly the link kinds StateItem already
#: expresses as fields (`depends_on`, `conflicts_with`, `supersedes`), the three
#: `assert_relationships_are_unique_and_non_self` traverses in `invariants.py`,
#: and the three `build_dependency_graph` emits. A Dependency records one of those
#: links; it may not invent a fourth. Until this was closed the relation of record
#: accepted any string, so the system could not decide whether two canonical
#: relations were the same relation, and every consistency guarantee layered above
#: it was nominal.
CANONICAL_RELATIONS = frozenset({"depends_on", "conflicts_with", "supersedes"})

# NOTE: Conflict.conflict_type is intentionally NOT sealed here. An earlier draft
# closed it onto {owner, version, value} and claimed each value was derived from
# an existing check; an audit found that no invariant reads or produces
# conflict_type (`assert_single_owner` and `assert_no_active_conflicting_versions`
# raise, they do not create Conflict records), and the only value used anywhere is
# "value" in three tests. Sealing it is a genuine new policy, so it is deferred to
# its own PROPOSED decision rather than smuggled in beside the well-grounded
# relation seal.


@dataclass(frozen=True, slots=True, kw_only=True)
class Entity(StateItem):
    """A named domain-neutral thing in the Canonical State."""

    name: str = ""
    entity_type: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        StateItem.__post_init__(self)
        if not self.name.strip() or not self.entity_type.strip():
            raise InvariantViolation("Entity에는 name과 entity_type이 필요합니다.")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True, kw_only=True)
class Fact(StateItem):
    """A claim about a subject, with provenance and explicit lifecycle."""

    subject_id: str = ""
    predicate: str = ""
    value: Any = None

    def __post_init__(self) -> None:
        StateItem.__post_init__(self)
        if not self.subject_id.strip() or not self.predicate.strip():
            raise InvariantViolation("Fact에는 subject_id와 predicate가 필요합니다.")


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision(StateItem):
    """A decision record with rationale and alternatives."""

    question: str = ""
    decision: str = ""
    rationale: str = ""
    alternatives: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        StateItem.__post_init__(self)
        if not self.question.strip() or not self.decision.strip():
            raise InvariantViolation("Decision에는 question과 decision이 필요합니다.")
        object.__setattr__(self, "alternatives", tuple(self.alternatives))


@dataclass(frozen=True, slots=True, kw_only=True)
class Dependency(StateItem):
    """A directed relation between two state records."""

    source_id: str = ""
    target_id: str = ""
    relation: str = "depends_on"

    def __post_init__(self) -> None:
        StateItem.__post_init__(self)
        if not self.source_id.strip() or not self.target_id.strip():
            raise InvariantViolation("Dependency에는 source_id와 target_id가 필요합니다.")
        if self.relation not in CANONICAL_RELATIONS:
            raise InvariantViolation(
                f"Dependency.relation은 {sorted(CANONICAL_RELATIONS)} 중 하나여야 합니다: "
                f"{self.relation!r}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Conflict(StateItem):
    """An unresolved or resolved conflict between two state records."""

    left_id: str = ""
    right_id: str = ""
    conflict_type: str = ""
    resolution: str | None = None

    def __post_init__(self) -> None:
        StateItem.__post_init__(self)
        if not self.left_id.strip() or not self.right_id.strip() or not self.conflict_type.strip():
            raise InvariantViolation(
                "Conflict에는 left_id, right_id, conflict_type이 필요합니다."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Invariant(StateItem):
    """A deterministic rule that can be evaluated without an LLM."""

    name: str = ""
    description: str = ""
    severity: str = "error"

    def __post_init__(self) -> None:
        StateItem.__post_init__(self)
        if not self.name.strip() or not self.description.strip():
            raise InvariantViolation("Invariant에는 name과 description이 필요합니다.")


@dataclass(frozen=True, slots=True, kw_only=True)
class Projection(StateItem):
    """A read-only view of Canonical State for one locale or task."""

    locale: str = ""
    purpose: str = ""
    canonical_revision: int = 0
    canonical_ids: tuple[str, ...] = ()
    content: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        StateItem.__post_init__(self)
        if not self.locale.strip() or not self.purpose.strip():
            raise InvariantViolation("Projection에는 locale과 purpose가 필요합니다.")
        if self.canonical_revision < 0:
            raise InvariantViolation("canonical_revision은 0 이상이어야 합니다.")
        object.__setattr__(self, "canonical_ids", tuple(self.canonical_ids))
        object.__setattr__(self, "content", MappingProxyType(dict(self.content)))


def _validate_identity(item_id: str, owner: str) -> None:
    if not item_id.strip():
        raise InvariantViolation("모든 상태 항목에는 id가 필요합니다.")
    if not owner.strip():
        raise InvariantViolation("모든 상태 항목에는 owner가 필요합니다.")


def to_record(value: Any) -> Any:
    """Serialize contracts into JSON/YAML-friendly primitives."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: to_record(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_record(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_record(item) for item in value]
    return value
