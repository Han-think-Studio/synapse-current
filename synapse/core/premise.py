"""Deterministic premise partition for IR candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from synapse.core.contracts import LifecycleStatus
from synapse.core.ir import SynapseIR


@dataclass(frozen=True, slots=True)
class PremisePartition:
    confirmed_ids: tuple[str, ...]
    assumed_ids: tuple[str, ...]
    proposed_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    blocked_ids: tuple[str, ...]
    reasons: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "confirmed_ids", tuple(self.confirmed_ids))
        object.__setattr__(self, "assumed_ids", tuple(self.assumed_ids))
        object.__setattr__(self, "proposed_ids", tuple(self.proposed_ids))
        object.__setattr__(self, "unresolved_ids", tuple(self.unresolved_ids))
        object.__setattr__(self, "blocked_ids", tuple(self.blocked_ids))
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))

    @property
    def counts(self) -> dict[str, int]:
        return {
            "CONFIRMED": len(self.confirmed_ids),
            "ASSUMED": len(self.assumed_ids),
            "PROPOSED": len(self.proposed_ids),
            "UNRESOLVED": len(self.unresolved_ids),
            "BLOCKED": len(self.blocked_ids),
        }

    def to_record(self) -> dict[str, object]:
        return {
            "confirmed_ids": list(self.confirmed_ids),
            "assumed_ids": list(self.assumed_ids),
            "proposed_ids": list(self.proposed_ids),
            "unresolved_ids": list(self.unresolved_ids),
            "blocked_ids": list(self.blocked_ids),
            "reasons": dict(self.reasons),
            "counts": self.counts,
        }


def partition_premises(ir: SynapseIR) -> PremisePartition:
    """Partition explicit lifecycle labels without inferring new truth."""
    buckets: dict[LifecycleStatus, list[str]] = {status: [] for status in LifecycleStatus}
    reasons: dict[str, str] = {}
    for node in ir.nodes:
        buckets[node.status].append(node.id)
        reasons[node.id] = f"explicit_status:{node.status.value}"
    return PremisePartition(
        confirmed_ids=tuple(buckets[LifecycleStatus.CONFIRMED]),
        assumed_ids=tuple(buckets[LifecycleStatus.ASSUMED]),
        proposed_ids=tuple(buckets[LifecycleStatus.PROPOSED]),
        unresolved_ids=tuple(buckets[LifecycleStatus.UNRESOLVED]),
        blocked_ids=tuple(buckets[LifecycleStatus.BLOCKED]),
        reasons=reasons,
    )


__all__ = ["PremisePartition", "partition_premises"]
