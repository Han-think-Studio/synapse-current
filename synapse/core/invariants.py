"""Deterministic Core invariants, including the Phase 31 reactive gate."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace

from synapse.core.contracts import Conflict, LifecycleStatus, Projection, Provenance, StateItem
from synapse.core.errors import InvariantViolation


def assert_single_owner(items: Iterable[StateItem]) -> None:
    """A Canonical State key may have one owner only."""
    owners_by_key: dict[str, set[str]] = defaultdict(set)
    for item in items:
        owners_by_key[item.canonical_key or item.id].add(item.owner)
    conflicts = {
        key: sorted(owners)
        for key, owners in owners_by_key.items()
        if len(owners) > 1
    }
    if conflicts:
        raise InvariantViolation(f"Canonical owner가 하나가 아닙니다: {conflicts}")


def assert_confirmed_has_provenance(items: Iterable[StateItem]) -> None:
    """Confirmed records must remain traceable to a source."""
    missing = [item.id for item in items if item.status is LifecycleStatus.CONFIRMED and not item.provenance]
    if missing:
        raise InvariantViolation(f"provenance 없는 CONFIRMED 항목: {missing}")


def assert_no_active_conflicting_versions(items: Iterable[StateItem]) -> None:
    """One ID cannot have multiple active versions at the same time."""
    active_by_id: dict[str, list[int]] = defaultdict(list)
    for item in items:
        if item.status is not LifecycleStatus.DEPRECATED:
            active_by_id[item.id].append(item.version)
    conflicts = {
        item_id: versions
        for item_id, versions in active_by_id.items()
        if len(versions) > 1
    }
    if conflicts:
        raise InvariantViolation(f"동시에 ACTIVE인 version이 존재합니다: {conflicts}")


def assert_relationships_are_unique_and_non_self(items: Iterable[StateItem]) -> None:
    """Relationship lists cannot hide duplicate or self-referential edges."""
    violations: list[str] = []
    for item in items:
        for relation in ("depends_on", "supersedes", "conflicts_with"):
            targets = tuple(getattr(item, relation))
            if len(targets) != len(set(targets)):
                violations.append(f"{item.id}.{relation}:duplicate")
            if item.id in targets:
                violations.append(f"{item.id}.{relation}:self")
    if violations:
        raise InvariantViolation(f"관계 edge가 유일하지 않거나 self를 가리킵니다: {violations}")


def assert_conflict_lifecycle(items: Iterable[StateItem]) -> None:
    """A Conflict's lifecycle must agree with whether it has a resolution."""
    violations: list[str] = []
    for item in items:
        if not isinstance(item, Conflict):
            continue
        if item.status is LifecycleStatus.UNRESOLVED and item.resolution is not None:
            violations.append(f"{item.id}:UNRESOLVED-with-resolution")
        if item.status is LifecycleStatus.CONFIRMED and not item.resolution:
            violations.append(f"{item.id}:CONFIRMED-without-resolution")
    if violations:
        raise InvariantViolation(f"Conflict lifecycle이 일치하지 않습니다: {violations}")


def assert_projection_is_read_only(projection: Projection) -> None:
    """Projection must be a frozen, non-authoritative view."""
    if not isinstance(projection, Projection):
        raise InvariantViolation("Projection 타입이 아닙니다.")
    if not projection.canonical_ids:
        raise InvariantViolation("Projection은 참조한 canonical_ids를 가져야 합니다.")


def assert_projection_targets_are_active(
    projection: Projection,
    items: Iterable[StateItem],
) -> None:
    """A runtime-facing projection cannot directly target deprecated state."""
    index = {item.id: item for item in items}
    deprecated = [
        item_id
        for item_id in projection.canonical_ids
        if index.get(item_id) is not None
        and index[item_id].status is LifecycleStatus.DEPRECATED
    ]
    if deprecated:
        raise InvariantViolation(f"DEPRECATED 항목은 Projection 대상이 될 수 없습니다: {deprecated}")


def validate_core(items: Iterable[StateItem]) -> None:
    """Run all Phase 1 invariants for a collection."""
    materialized = list(items)
    assert_single_owner(materialized)
    assert_confirmed_has_provenance(materialized)
    assert_no_active_conflicting_versions(materialized)
    assert_relationships_are_unique_and_non_self(materialized)
    assert_conflict_lifecycle(materialized)


def validate_propagation(before_items: Iterable[StateItem], result: object) -> None:
    """Validate a Resolver result against the state it was derived from.

    This is the Phase 31 enforcement boundary for INV-011 through INV-014.  It
    intentionally validates a detached result before a Registry swaps any of
    its mutable containers.  A repair may change the affected item, but every
    other item must remain byte-for-byte equivalent as a Python value.
    """
    from synapse.core.resolver import PropagationResult

    if not isinstance(result, PropagationResult):
        raise InvariantViolation("PropagationResult 타입이 아닙니다.")

    before = _state_item_index(before_items, label="before")
    if not isinstance(result.items, Mapping):
        raise InvariantViolation("PropagationResult.items는 mapping이어야 합니다.")
    after = dict(result.items)
    if any(not isinstance(item_id, str) for item_id in after):
        raise InvariantViolation("PropagationResult.items key는 문자열이어야 합니다.")
    if any(not isinstance(item, StateItem) or item_id != item.id for item_id, item in after.items()):
        raise InvariantViolation("PropagationResult.items key와 StateItem id가 일치하지 않습니다.")
    if set(before) != set(after):
        raise InvariantViolation("PropagationResult가 item을 추가하거나 누락했습니다.")
    validate_core(before.values())
    validate_core(after.values())

    changes = tuple(result.changes)
    if any(not hasattr(change, "id") or not isinstance(change.id, str) for change in changes):
        raise InvariantViolation("PropagationResult Change id가 올바르지 않습니다.")
    change_ids = [change.id for change in changes]
    if len(change_ids) != len(set(change_ids)):
        raise InvariantViolation("PropagationResult Change id가 중복됩니다.")

    for change in changes:
        _validate_propagation_change(change, before, after)

    for item_id, original in before.items():
        if item_id not in set(change_ids) and after[item_id] != original:
            raise InvariantViolation(f"미처리 항목이 propagation 중 변경되었습니다: {item_id}")

    for item_id, original in before.items():
        if isinstance(original, Conflict) and original.status is LifecycleStatus.UNRESOLVED:
            current = after[item_id]
            if (
                not isinstance(current, Conflict)
                or current.status is not LifecycleStatus.UNRESOLVED
                or current != original
            ):
                raise InvariantViolation(f"UNRESOLVED Conflict가 propagation 중 보존되지 않았습니다: {item_id}")


def _state_item_index(items: Iterable[StateItem], *, label: str) -> dict[str, StateItem]:
    index: dict[str, StateItem] = {}
    for item in items:
        if not isinstance(item, StateItem):
            raise InvariantViolation(f"{label}에 StateItem이 아닌 값이 있습니다.")
        if item.id in index:
            raise InvariantViolation(f"{label}에 중복 item id가 있습니다: {item.id}")
        index[item.id] = item
    return index


def _validate_propagation_change(
    change: object,
    before: Mapping[str, StateItem],
    after: Mapping[str, StateItem],
) -> None:
    """Check one Change and its before/after StateItem pair."""
    from synapse.core.resolver import Change

    if not isinstance(change, Change):
        raise InvariantViolation("PropagationResult에 알 수 없는 Change가 있습니다.")
    original = before.get(change.id)
    current = after.get(change.id)
    if original is None or current is None:
        raise InvariantViolation(f"Change가 알 수 없는 item을 가리킵니다: {change.id}")
    if original.status is not change.before or current.status is not change.after:
        raise InvariantViolation(f"Change status와 StateItem status가 일치하지 않습니다: {change.id}")
    if change.trigger_id not in before:
        raise InvariantViolation(f"Change trigger가 알 수 없습니다: {change.trigger_id}")
    if original.status not in (LifecycleStatus.CONFIRMED, LifecycleStatus.ASSUMED):
        raise InvariantViolation(f"비활성 item을 propagation이 재평가했습니다: {change.id}")
    if original == current:
        raise InvariantViolation(f"실제 변경 없는 Change가 보고되었습니다: {change.id}")

    if change.repaired:
        provenance = change.repair_provenance
        if original.status is not LifecycleStatus.CONFIRMED:
            raise InvariantViolation(f"ASSUMED item을 새로 CONFIRMED로 유지할 수 없습니다: {change.id}")
        if current.status is not LifecycleStatus.CONFIRMED:
            raise InvariantViolation(f"repaired Change는 CONFIRMED여야 합니다: {change.id}")
        if not isinstance(provenance, Provenance):
            raise InvariantViolation(f"repaired Change에 repair provenance가 없습니다: {change.id}")
        if provenance in original.provenance:
            raise InvariantViolation(f"repair provenance가 새로 추가되지 않았습니다: {change.id}")
        if provenance not in current.provenance:
            raise InvariantViolation(f"repair provenance가 결과 StateItem에 없습니다: {change.id}")
        if not provenance.source_id.startswith("resolver:rule:") or not provenance.source_id.removeprefix("resolver:rule:").strip():
            raise InvariantViolation(f"repair provenance rule id가 없습니다: {change.id}")
        if provenance.method != "auto_repair":
            raise InvariantViolation(f"repair provenance method가 잘못되었습니다: {change.id}")
        if not isinstance(provenance.locator, str) or change.trigger_id not in provenance.locator:
            raise InvariantViolation(f"repair provenance trigger가 없습니다: {change.id}")
        if change.repair_provenance != provenance:
            raise InvariantViolation(f"Change와 StateItem의 repair provenance가 다릅니다: {change.id}")
        return

    if change.repair_provenance is not None:
        raise InvariantViolation(f"보수적 Change에는 repair provenance가 있을 수 없습니다: {change.id}")
    if current.status not in (LifecycleStatus.UNRESOLVED, LifecycleStatus.BLOCKED):
        raise InvariantViolation(f"보수적 Change가 안전한 상태로 재마크되지 않았습니다: {change.id}")
    if current.provenance != original.provenance:
        raise InvariantViolation(f"보수적 Change가 provenance를 변경했습니다: {change.id}")
    if current != replace(original, status=current.status):
        raise InvariantViolation(f"보수적 Change가 status 외 값을 변경했습니다: {change.id}")
