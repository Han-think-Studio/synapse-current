"""Read-only evidence frames for later TableCard selection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from synapse.core.ir import IRError, SynapseIR
from synapse.core.premise import PremisePartition, partition_premises


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    node_id: str
    source_id: str
    locator: str | None
    method: str


@dataclass(frozen=True, slots=True)
class CognitiveFrame:
    id: str
    subject_ids: tuple[str, ...]
    focus: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    premise: PremisePartition
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject_ids", tuple(self.subject_ids))
        object.__setattr__(self, "focus", tuple(self.focus))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "subject_ids": list(self.subject_ids),
            "focus": list(self.focus),
            "evidence": [
                {
                    "node_id": ref.node_id,
                    "source_id": ref.source_id,
                    "locator": ref.locator,
                    "method": ref.method,
                }
                for ref in self.evidence
            ],
            "premise": self.premise.to_record(),
            "metadata": dict(self.metadata),
        }


def build_cognitive_frame(
    ir: SynapseIR,
    *,
    focus: tuple[str, ...] = (),
    premise: PremisePartition | None = None,
) -> CognitiveFrame:
    """Build a deterministic evidence frame without changing IR or state."""
    ir.validate(strict=True)
    node_ids = set(ir.node_ids)
    unknown_focus = sorted(set(focus) - node_ids)
    if unknown_focus:
        raise IRError(f"Cognitive Frame focus에 없는 node가 있습니다: {unknown_focus}")
    partition = premise or partition_premises(ir)
    refs = tuple(
        EvidenceRef(
            node_id=node.id,
            source_id=provenance.source_id,
            locator=provenance.locator,
            method=provenance.method,
        )
        for node in ir.nodes
        for provenance in node.provenance
    )
    canonical = json.dumps(ir.to_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    frame_id = f"frame:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return CognitiveFrame(
        id=frame_id,
        subject_ids=ir.node_ids,
        focus=tuple(focus),
        evidence=refs,
        premise=partition,
        metadata={
            "ir_version": ir.version,
            "node_count": len(ir.nodes),
            "relation_count": len(ir.relations),
            "unresolved_preserved": True,
        },
    )


__all__ = ["CognitiveFrame", "EvidenceRef", "build_cognitive_frame"]
