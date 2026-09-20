"""Reactive propagation — the Resolver (Phase 3, contract: spec/reactive_integrity.yaml).

Deterministic, LLM-free. When one Canonical item changes, this re-evaluates the
items connected to it and either repairs them (rule-based, keeping CONFIRMED with
recorded provenance) or conservatively re-marks them (BLOCKED / UNRESOLVED),
preserving what cannot be resolved so the structure is never silently broken.

This module is pure: it reads a set of items and returns a new set plus a report.
It does not mutate a registry. Wiring into the commit path is a separate step.
"""

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from inspect import Parameter, signature

from synapse.core.contracts import LifecycleStatus, Provenance, StateItem
from synapse.core.repair_rules import RepairContext, build_repair_context

# A repair rule keyed by entity_type. Given the affected dependent and the id of
# the item that triggered the change, it returns a still-CONFIRMED replacement
# (which MUST carry a new Provenance for the repair) or None to fall back to the
# conservative outcome. See resolution_v1.rule_interface in the contract.
RepairRule = Callable[..., StateItem | None]


class ResolverBudgetExceeded(RuntimeError):
    """Raised when one propagation exceeds its explicit remark budget."""


@lru_cache(maxsize=128)
def _cached_rule_accepts_context(rule: object) -> bool:
    try:
        parameters = signature(rule).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is Parameter.VAR_KEYWORD
        or (
            parameter.name == "context"
            and parameter.kind
            in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)
        )
        for parameter in parameters
    )


def _rule_accepts_context(rule: object) -> bool:
    try:
        return _cached_rule_accepts_context(rule)
    except TypeError:  # Some callable instances are deliberately unhashable.
        return _cached_rule_accepts_context.__wrapped__(rule)


def _invoke_repair_rule(
    rule: RepairRule,
    dependent: StateItem,
    trigger_id: str,
    context: RepairContext,
) -> StateItem | None:
    if _rule_accepts_context(rule):
        return rule(dependent, trigger_id, context=context)
    return rule(dependent, trigger_id)

# Only items in these active statuses are re-evaluated; anything already
# UNRESOLVED / BLOCKED / DEPRECATED is left untouched.
_ACTIVE = (LifecycleStatus.CONFIRMED, LifecycleStatus.ASSUMED)


@dataclass(frozen=True, slots=True)
class Change:
    """One re-marked or repaired item produced by propagation."""

    id: str
    before: LifecycleStatus
    after: LifecycleStatus
    repaired: bool  # True = kept CONFIRMED by a rule; False = conservatively re-marked
    trigger_id: str = ""
    repair_provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class PropagationResult:
    items: Mapping[str, StateItem]
    changes: tuple[Change, ...] = ()

    @property
    def changed_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.changes)


def repair_keeping_confirmed(dependent: StateItem, trigger_id: str, *, rule_id: str) -> StateItem:
    """Helper for repair rules: keep the dependent CONFIRMED and record why."""
    prov = Provenance(
        source_id=f"resolver:rule:{rule_id}",
        method="auto_repair",
        locator=f"trigger={trigger_id}",
    )
    return replace(dependent, provenance=(*dependent.provenance, prov))


def _affected(index: Mapping[str, StateItem], changed_id: str) -> list[StateItem]:
    """Items that declare changed_id as a dependency or a conflict."""
    return [
        item
        for item in index.values()
        if changed_id in item.depends_on or changed_id in item.conflicts_with
    ]


def _reevaluate(
    dependent: StateItem,
    trigger_id: str,
    index: Mapping[str, StateItem],
    mode: str,
    rules: Mapping[str, RepairRule] | None,
    successor_id: str | None,
) -> StateItem:
    if dependent.status not in _ACTIVE:
        return dependent  # nothing to do; leave preserved states as-is

    if mode == "rule_based" and rules:
        rule = rules.get(dependent.entity_type)
        trigger = index.get(trigger_id)
        if rule is not None and trigger is not None:
            context = build_repair_context(
                trigger_item=trigger,
                successor_id=successor_id,
                dependent_id=dependent.id,
                items=index,
            )
            repaired = _invoke_repair_rule(rule, dependent, trigger_id, context)
            if repaired is not None:
                return repaired  # kept CONFIRMED (rule is responsible for provenance)

    # Conservative outcome. A DEPRECATED hard dependency means the foundation is
    # gone -> BLOCKED; an updated dependency or a conflict is still resolvable
    # but must be re-verified -> UNRESOLVED.
    trigger = index.get(trigger_id)
    hard_dependency_gone = (
        trigger_id in dependent.depends_on
        and trigger is not None
        and trigger.status is LifecycleStatus.DEPRECATED
    )
    new_status = LifecycleStatus.BLOCKED if hard_dependency_gone else LifecycleStatus.UNRESOLVED
    return replace(dependent, status=new_status)


def propagate(
    items: Iterable[StateItem],
    changed_id: str,
    *,
    mode: str = "conservative",
    rules: Mapping[str, RepairRule] | None = None,
    max_remarks: int = 10_000,
    successor_id: str | None = None,
) -> PropagationResult:
    """Ripple a change outward from ``changed_id`` and return the reconciled set.

    ``mode`` is ``"conservative"`` (re-mark only) or ``"rule_based"`` (try repair
    rules, then fall back to conservative). Terminating: each item is
    re-evaluated at most once, so cycles are safe.
    """
    if not isinstance(max_remarks, int) or isinstance(max_remarks, bool) or max_remarks < 0:
        raise ValueError("max_remarks는 0 이상의 정수여야 합니다.")
    index: dict[str, StateItem] = {item.id: item for item in items}
    visited: set[str] = set()
    queue: deque[str] = deque([changed_id])
    changes: list[Change] = []

    while queue:
        current = queue.popleft()
        for dependent in _affected(index, current):
            if dependent.id in visited:
                continue
            visited.add(dependent.id)
            updated = _reevaluate(dependent, current, index, mode, rules, successor_id)
            if updated is dependent:
                continue  # not active / no change
            if len(changes) >= max_remarks:
                raise ResolverBudgetExceeded(
                    f"Resolver propagation remark budget exceeded: {max_remarks}"
                )
            index[dependent.id] = updated
            added_provenance = tuple(
                provenance
                for provenance in updated.provenance
                if provenance not in dependent.provenance
            )
            repaired = updated.status is LifecycleStatus.CONFIRMED
            changes.append(
                Change(
                    id=dependent.id,
                    before=dependent.status,
                    after=updated.status,
                    repaired=repaired,
                    trigger_id=current,
                    repair_provenance=added_provenance[-1] if repaired and added_provenance else None,
                )
            )
            queue.append(dependent.id)  # ripple onward to its own dependents

    return PropagationResult(items=index, changes=tuple(changes))
