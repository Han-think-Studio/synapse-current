"""Explicit, deterministic repair-rule contracts for Phase 32.

Rules receive a deliberately small immutable context.  The module contains one
opt-in rule only; a default Registry remains conservative unless the caller
passes ``BUILT_IN_RULES`` explicitly.
"""

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from synapse.core.contracts import LifecycleStatus, Provenance, StateItem
from synapse.core.errors import InvariantViolation


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairContext:
    """The minimum read-only information disclosed to a context-aware rule."""

    trigger_item: StateItem
    successor_id: str | None
    would_create_cycle: bool

    def __post_init__(self) -> None:
        if not isinstance(self.trigger_item, StateItem):
            raise InvariantViolation("RepairContext trigger_item은 StateItem이어야 합니다.")
        if self.successor_id is not None and (
            not isinstance(self.successor_id, str) or not self.successor_id.strip()
        ):
            raise InvariantViolation("RepairContext successor_id가 올바르지 않습니다.")
        if not isinstance(self.would_create_cycle, bool):
            raise InvariantViolation("RepairContext would_create_cycle은 bool이어야 합니다.")


RepairRuleV2 = Callable[..., StateItem | None]


def would_create_dependency_cycle(
    items: Mapping[str, StateItem],
    dependent_id: str,
    successor_id: str | None,
) -> bool:
    """Return whether adding ``dependent -> successor`` closes a dep cycle."""
    if not successor_id:
        return False
    if successor_id == dependent_id:
        return True

    visited: set[str] = set()
    queue: deque[str] = deque([successor_id])
    while queue:
        current_id = queue.popleft()
        if current_id in visited:
            continue
        visited.add(current_id)
        if current_id == dependent_id:
            return True
        current = items.get(current_id)
        if current is None:
            continue
        for next_id in current.depends_on:
            if next_id not in visited:
                queue.append(next_id)
    return False


def build_repair_context(
    *,
    trigger_item: StateItem,
    successor_id: str | None,
    dependent_id: str,
    items: Mapping[str, StateItem],
) -> RepairContext:
    """Build a context from read-only state and an explicit successor id."""
    known_successor = successor_id if successor_id in items else None
    return RepairContext(
        trigger_item=trigger_item,
        successor_id=known_successor,
        would_create_cycle=would_create_dependency_cycle(items, dependent_id, known_successor),
    )


def supersede_relink(
    dependent: StateItem,
    trigger_id: str,
    *,
    context: RepairContext,
) -> StateItem | None:
    """Replace one deprecated dependency with its explicit successor.

    The rule is intentionally narrow.  An ``ASSUMED`` dependent falls back to
    the conservative path because Phase 31 forbids a new ``CONFIRMED`` status
    from an ``ASSUMED`` input.
    """
    successor_id = context.successor_id
    if context.trigger_item.id != trigger_id:
        return None
    if context.trigger_item.status is not LifecycleStatus.DEPRECATED:
        return None
    if dependent.status is not LifecycleStatus.CONFIRMED:
        return None
    if successor_id is None or successor_id in {trigger_id, dependent.id}:
        return None
    if trigger_id not in dependent.depends_on:
        return None
    if successor_id in dependent.depends_on:
        return None
    if context.would_create_cycle:
        return None

    replacement = tuple(
        successor_id if dependency_id == trigger_id else dependency_id
        for dependency_id in dependent.depends_on
    )
    if len(replacement) != len(dependent.depends_on) or len(replacement) != len(set(replacement)):
        return None

    repair_provenance = Provenance(
        source_id="resolver:rule:supersede_relink",
        method="auto_repair",
        locator=f"trigger={trigger_id};successor={successor_id}",
    )
    return replace(
        dependent,
        depends_on=replacement,
        provenance=(*dependent.provenance, repair_provenance),
    )


BUILT_IN_RULES: Mapping[str, RepairRuleV2] = MappingProxyType(
    {
        "fact": supersede_relink,
        "document": supersede_relink,
    }
)


__all__ = [
    "BUILT_IN_RULES",
    "RepairContext",
    "RepairRuleV2",
    "build_repair_context",
    "supersede_relink",
    "would_create_dependency_cycle",
]
