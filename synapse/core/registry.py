"""Canonical Registry and proposal boundary.

This is the first commit boundary after the Phase 1 contracts. A candidate is
not Canonical State merely because it is structurally valid; it must pass the
deterministic validator and be committed with a matching validation receipt.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any

from synapse.core.contracts import LifecycleStatus, Provenance, StateItem
from synapse.core.errors import InvariantViolation
from synapse.core.invariants import validate_core, validate_propagation
from synapse.core.resolver import RepairRule, propagate


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProposalOperation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    SUPERSEDE = "SUPERSEDE"
    DEPRECATE = "DEPRECATE"


@dataclass(frozen=True, slots=True, kw_only=True)
class Proposal:
    """A candidate change waiting for validation and commit."""

    candidate: StateItem
    operation: ProposalOperation
    actor: str
    id: str = ""
    created_at: str = field(default_factory=_now)
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.actor.strip():
            raise InvariantViolation("Proposal에는 actor가 필요합니다.")
        if not self.id:
            object.__setattr__(self, "id", f"proposal:{self.candidate.id}:{self.operation.value.lower()}")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposalValidation:
    proposal_id: str
    passed: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


# Domain packs may contribute deterministic, LLM-free proposal guardrails.  A
# rule returns human-readable errors; an empty result means it has no objection
# to this proposal (including proposals belonging to another domain pack).
ProposalRule = Callable[[Proposal, "CanonicalRegistry"], Iterable[str] | None]


@dataclass(frozen=True, slots=True, kw_only=True)
class RegistrySnapshot:
    revision: int
    items: Any


@dataclass(frozen=True, slots=True, kw_only=True)
class ReactiveMark:
    """Durable evidence of one deterministic Resolver re-evaluation."""

    id: str
    trigger_id: str
    before: LifecycleStatus
    after: LifecycleStatus
    repaired: bool
    revision: int
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.trigger_id.strip():
            raise InvariantViolation("ReactiveMark에는 id와 trigger_id가 필요합니다.")
        if self.revision < 1:
            raise InvariantViolation("ReactiveMark revision은 1 이상이어야 합니다.")
        if self.repaired:
            if self.after is not LifecycleStatus.CONFIRMED or self.provenance is None:
                raise InvariantViolation("repaired ReactiveMark에는 CONFIRMED와 repair provenance가 필요합니다.")
            if not self.provenance.source_id.startswith("resolver:rule:"):
                raise InvariantViolation("repair provenance source_id가 resolver rule이 아닙니다.")
            if self.provenance.method != "auto_repair":
                raise InvariantViolation("repair provenance method가 auto_repair가 아닙니다.")
            if (
                not isinstance(self.provenance.locator, str)
                or f"trigger={self.trigger_id}" not in self.provenance.locator
            ):
                raise InvariantViolation("repair provenance trigger가 ReactiveMark와 다릅니다.")
        elif self.after not in (LifecycleStatus.UNRESOLVED, LifecycleStatus.BLOCKED):
            raise InvariantViolation("보수적 ReactiveMark는 UNRESOLVED 또는 BLOCKED여야 합니다.")


class CanonicalRegistry:
    """Own the current Canonical State and its revision history."""

    def __init__(self, repair_rules: Mapping[str, RepairRule] | None = None) -> None:
        self._items: dict[str, StateItem] = {}
        self._history: dict[str, list[StateItem]] = {}
        self._revision = 0
        # ProposalValidation is an in-memory receipt.  Keep the exact object
        # issued for a Registry revision so a stale or caller-forged receipt
        # cannot authorize a later Canonical transition.
        self._validation_receipts: dict[
            int, tuple[ProposalValidation, Proposal, int]
        ] = {}
        # Optional deterministic repair rules keyed by entity_type (DEC-0006).
        # Empty -> reactive propagation falls back to conservative re-marking.
        self._repair_rules: dict[str, RepairRule] = dict(repair_rules or {})
        self._reactive_marks: list[ReactiveMark] = []

    @property
    def revision(self) -> int:
        return self._revision

    def get(self, item_id: str) -> StateItem | None:
        return self._items.get(item_id)

    def all(self) -> tuple[StateItem, ...]:
        return tuple(self._items.values())

    def history(self, item_id: str) -> tuple[StateItem, ...]:
        return tuple(self._history.get(item_id, ()))

    def reactive_marks(self) -> tuple[ReactiveMark, ...]:
        return tuple(self._reactive_marks)

    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(
            revision=self._revision,
            items=MappingProxyType(dict(self._items)),
        )

    def _restore(
        self,
        *,
        items: Mapping[str, StateItem],
        history: Mapping[str, Iterable[StateItem]],
        revision: int,
        reactive_marks: Iterable[ReactiveMark] = (),
    ) -> None:
        """Restore a previously validated durable state.

        Persistence is the only caller of this internal boundary. Restoring is
        deliberately not expressed as a series of commits: replaying commits
        would re-run reactions and manufacture a new revision instead of
        reopening the exact state that was saved.
        """
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise InvariantViolation("복원 revision은 0 이상의 정수여야 합니다.")
        materialized = dict(items)
        if set(materialized) != {item.id for item in materialized.values()}:
            raise InvariantViolation("복원 items의 key와 id가 일치하지 않습니다.")
        history_map = {str(item_id): list(records) for item_id, records in history.items()}
        for item_id, records in history_map.items():
            if not item_id.strip() or any(record.id != item_id for record in records):
                raise InvariantViolation("복원 history의 key와 record id가 일치하지 않습니다.")
        validate_core(materialized.values())
        marks = tuple(reactive_marks)
        mark_keys = [(mark.revision, mark.id, mark.trigger_id) for mark in marks]
        if len(mark_keys) != len(set(mark_keys)):
            raise InvariantViolation("복원 ReactiveMark가 중복됩니다.")
        if any(mark.revision > revision for mark in marks):
            raise InvariantViolation("복원 ReactiveMark revision이 현재 revision보다 큽니다.")
        known_ids = set(materialized) | set(history_map)
        if any(mark.id not in known_ids or mark.trigger_id not in known_ids for mark in marks):
            raise InvariantViolation("복원 ReactiveMark가 알 수 없는 item을 가리킵니다.")
        self._items = materialized
        self._history = history_map
        self._revision = revision
        self._reactive_marks = list(marks)
        self._validation_receipts.clear()

    def _remember_validation(self, proposal: Proposal, validation: ProposalValidation) -> None:
        """Bind one validator-issued receipt to its Proposal and Registry revision."""

        self._validation_receipts[id(validation)] = (validation, proposal, self._revision)

    def commit(self, proposal: Proposal, validation: ProposalValidation) -> StateItem:
        """Commit only the exact proposal that received a passing receipt."""
        receipt = self._validation_receipts.get(id(validation))
        if (
            validation.proposal_id != proposal.id
            or not validation.passed
            or receipt is None
            or receipt[0] is not validation
            or receipt[1] is not proposal
            or receipt[2] != self._revision
        ):
            raise InvariantViolation("통과한 ProposalValidation 없이 Canonical State를 commit할 수 없습니다.")

        operation = proposal.operation
        candidate = proposal.candidate
        target_id = candidate.supersedes[0] if candidate.supersedes else candidate.id
        current = self._items.get(target_id)

        # Work entirely on detached containers.  No Registry-owned container is
        # swapped until the operation and all reactive checks have passed.
        next_items = dict(self._items)
        next_history = {item_id: list(records) for item_id, records in self._history.items()}
        next_reactive_marks = list(self._reactive_marks)

        if operation is ProposalOperation.CREATE:
            if current is not None:
                raise InvariantViolation(f"이미 존재하는 상태 항목입니다: {candidate.id}")
            next_items[candidate.id] = candidate
        elif operation is ProposalOperation.UPDATE:
            if current is None:
                raise InvariantViolation(f"업데이트 대상이 없습니다: {candidate.id}")
            next_history.setdefault(candidate.id, []).append(current)
            next_items[candidate.id] = candidate
        elif operation is ProposalOperation.SUPERSEDE:
            if current is None:
                raise InvariantViolation(f"대체 대상이 없습니다: {target_id}")
            deprecated = replace(current, status=LifecycleStatus.DEPRECATED, updated_at=_now())
            next_history.setdefault(current.id, []).append(deprecated)
            next_items[current.id] = deprecated
            if candidate.id in next_items:
                raise InvariantViolation(f"대체 항목 id가 이미 존재합니다: {candidate.id}")
            next_items[candidate.id] = candidate
        elif operation is ProposalOperation.DEPRECATE:
            if current is None:
                raise InvariantViolation(f"폐기 대상이 없습니다: {candidate.id}")
            next_history.setdefault(candidate.id, []).append(current)
            next_items[candidate.id] = replace(
                candidate,
                status=LifecycleStatus.DEPRECATED,
                updated_at=_now(),
            )
        else:
            raise InvariantViolation(f"지원하지 않는 Proposal operation입니다: {operation}")

        validate_core(next_items.values())
        next_items, next_history, next_reactive_marks = self._resolve_reactions(
            operation,
            candidate,
            current,
            items=next_items,
            history=next_history,
            reactive_marks=next_reactive_marks,
        )

        self._items = next_items
        self._history = next_history
        self._reactive_marks = next_reactive_marks
        self._revision += 1
        self._validation_receipts.pop(id(validation), None)
        return self._items[candidate.id]

    def _resolve_reactions(
        self,
        operation: "ProposalOperation",
        candidate: StateItem,
        current: StateItem | None,
        *,
        items: dict[str, StateItem],
        history: dict[str, list[StateItem]],
        reactive_marks: list[ReactiveMark],
    ) -> tuple[dict[str, StateItem], dict[str, list[StateItem]], list[ReactiveMark]]:
        """Reactively re-mark/repair items connected to the changed one (DEC-0006).

        Runs for the operations that change an existing item (UPDATE, SUPERSEDE,
        DEPRECATE). The re-marks belong to the SAME revision as the change that
        caused them — one logical edit, even though it rippled.
        """
        if operation not in (
            ProposalOperation.UPDATE,
            ProposalOperation.SUPERSEDE,
            ProposalOperation.DEPRECATE,
        ):
            return items, history, reactive_marks
        # For SUPERSEDE the reactive trigger is the now-deprecated old item, so
        # its dependents see a lost foundation; otherwise it is the changed item.
        trigger_id = current.id if operation is ProposalOperation.SUPERSEDE and current else candidate.id
        mode = "rule_based" if self._repair_rules else "conservative"
        result = propagate(
            items.values(),
            trigger_id,
            mode=mode,
            rules=self._repair_rules,
            successor_id=candidate.id if operation is ProposalOperation.SUPERSEDE else None,
        )
        validate_propagation(items.values(), result)
        target_revision = self._revision + 1
        for change in result.changes:
            history.setdefault(change.id, []).append(items[change.id])
            items[change.id] = result.items[change.id]
            reactive_marks.append(
                ReactiveMark(
                    id=change.id,
                    trigger_id=change.trigger_id,
                    before=change.before,
                    after=change.after,
                    repaired=change.repaired,
                    revision=target_revision,
                    provenance=change.repair_provenance,
                )
            )
        if result.changes:
            validate_core(items.values())
        return items, history, reactive_marks


class ProposalValidator:
    """Validate proposal semantics without invoking an LLM."""

    def __init__(self, validators: Iterable[ProposalRule] = ()) -> None:
        self._validators: list[ProposalRule] = []
        for validator in validators:
            self.register(validator)

    def register(self, validator: ProposalRule) -> None:
        """Register one deterministic domain guardrail.

        Registration is explicit: importing a domain pack never changes the
        Core validator's behavior or the Canonical Registry by itself.
        """
        if not callable(validator):
            raise TypeError("Proposal validator는 callable이어야 합니다.")
        self._validators.append(validator)

    def validate(self, proposal: Proposal, registry: CanonicalRegistry) -> ProposalValidation:
        candidate = proposal.candidate
        target_id = candidate.supersedes[0] if candidate.supersedes else candidate.id
        current = registry.get(target_id)
        errors: list[str] = []
        warnings: list[str] = []

        if candidate.status is LifecycleStatus.PROPOSED:
            errors.append("PROPOSED 상태는 검증 없이 Canonical State로 commit할 수 없습니다.")
        if candidate.status is LifecycleStatus.BLOCKED:
            errors.append("BLOCKED 상태는 injection candidate로 commit할 수 없습니다.")

        if proposal.operation is ProposalOperation.CREATE:
            if current is not None:
                errors.append(f"CREATE 대상이 이미 존재합니다: {candidate.id}")
            trial_items = [*registry.all(), candidate]
        elif proposal.operation is ProposalOperation.UPDATE:
            if current is None:
                errors.append(f"UPDATE 대상이 없습니다: {candidate.id}")
                trial_items = [*registry.all()]
            else:
                if candidate.owner != current.owner:
                    errors.append("Canonical State owner를 변경할 수 없습니다.")
                if candidate.version <= current.version:
                    errors.append("UPDATE version은 현재 version보다 커야 합니다.")
                trial_items = [candidate if item.id == current.id else item for item in registry.all()]
        elif proposal.operation is ProposalOperation.SUPERSEDE:
            if current is None:
                errors.append(f"SUPERSEDE 대상이 없습니다: {target_id}")
                trial_items = [*registry.all()]
            else:
                if candidate.id in {item.id for item in registry.all()}:
                    errors.append(f"SUPERSEDE 새 id가 이미 존재합니다: {candidate.id}")
                if candidate.owner != current.owner:
                    errors.append("SUPERSEDE owner는 기존 owner와 같아야 합니다.")
                if candidate.version <= current.version:
                    errors.append("SUPERSEDE version은 기존 version보다 커야 합니다.")
                if current.id not in candidate.supersedes:
                    errors.append("SUPERSEDE candidate에는 기존 id가 supersedes로 기록되어야 합니다.")
                deprecated = replace(current, status=LifecycleStatus.DEPRECATED)
                trial_items = [deprecated if item.id == current.id else item for item in registry.all()]
                trial_items.append(candidate)
        elif proposal.operation is ProposalOperation.DEPRECATE:
            if current is None:
                errors.append(f"DEPRECATE 대상이 없습니다: {candidate.id}")
                trial_items = [*registry.all()]
            else:
                if candidate.owner != current.owner:
                    errors.append("DEPRECATE owner는 기존 owner와 같아야 합니다.")
                trial_items = [replace(current, status=LifecycleStatus.DEPRECATED)]
                trial_items.extend(item for item in registry.all() if item.id != current.id)
        else:
            errors.append(f"지원하지 않는 Proposal operation입니다: {proposal.operation}")
            trial_items = [*registry.all()]

        try:
            validate_core(trial_items)
        except InvariantViolation as exc:
            errors.append(str(exc))

        for validator in self._validators:
            try:
                rule_errors = validator(proposal, registry)
            except InvariantViolation as exc:
                errors.append(str(exc))
                continue
            except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
                name = getattr(validator, "__name__", validator.__class__.__name__)
                errors.append(f"Proposal validator {name} 실행 실패: {exc}")
                continue
            if rule_errors is None:
                continue
            if isinstance(rule_errors, str):
                errors.append(rule_errors)
            else:
                errors.extend(str(error) for error in rule_errors if str(error).strip())

        validation = ProposalValidation(
            proposal_id=proposal.id,
            passed=not errors,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )
        if validation.passed:
            registry._remember_validation(proposal, validation)
        return validation
