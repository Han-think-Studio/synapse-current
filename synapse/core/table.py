"""Structured Cognitive Table assembly and deterministic result merging."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.cards import CardRoute
from synapse.core.cognitive import CognitiveFrame


class CognitiveTableError(ValueError):
    """Raised when a Cognitive Table package or result is invalid."""


@dataclass(frozen=True, slots=True)
class TableCell:
    card_id: str
    status: str
    focus: tuple[str, ...]
    anti_focus: tuple[str, ...]
    output: tuple[str, ...]
    evidence_requirements: tuple[str, ...]
    supplied_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.card_id.strip() or self.status not in {"READY", "ABSTAIN"}:
            raise CognitiveTableError("TableCell card_id/status가 잘못되었습니다.")
        for name in (
            "focus",
            "anti_focus",
            "output",
            "evidence_requirements",
            "supplied_evidence",
            "missing_evidence",
        ):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))

    def to_record(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "status": self.status,
            "focus": list(self.focus),
            "anti_focus": list(self.anti_focus),
            "output": list(self.output),
            "evidence_requirements": list(self.evidence_requirements),
            "supplied_evidence": list(self.supplied_evidence),
            "missing_evidence": list(self.missing_evidence),
        }


@dataclass(frozen=True, slots=True)
class CardResult:
    card_id: str
    claims: Mapping[str, str]
    evidence_ids: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.card_id.strip():
            raise CognitiveTableError("CardResult card_id가 비어 있습니다.")
        normalized = {
            str(key).strip(): str(value).strip()
            for key, value in self.claims.items()
            if str(key).strip() and str(value).strip()
        }
        object.__setattr__(self, "claims", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "evidence_ids", tuple(sorted(set(self.evidence_ids))))
        object.__setattr__(self, "unresolved", tuple(sorted(set(self.unresolved))))

    def to_record(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "claims": dict(self.claims),
            "evidence_ids": list(self.evidence_ids),
            "unresolved": list(self.unresolved),
        }


@dataclass(frozen=True, slots=True)
class CognitiveTable:
    id: str
    frame_id: str
    event_id: str
    cells: tuple[TableCell, ...]
    consensus: Mapping[str, str]
    conflicts: Mapping[str, tuple[str, ...]]
    unresolved: tuple[str, ...]
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.frame_id.strip() or not self.event_id.strip():
            raise CognitiveTableError("CognitiveTable 식별자가 비어 있습니다.")
        cells = tuple(sorted(self.cells, key=lambda cell: cell.card_id))
        if len({cell.card_id for cell in cells}) != len(cells):
            raise CognitiveTableError("CognitiveTable cell card_id가 중복됩니다.")
        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "consensus", MappingProxyType(dict(sorted(self.consensus.items()))))
        object.__setattr__(
            self,
            "conflicts",
            MappingProxyType({key: tuple(sorted(set(values))) for key, values in sorted(self.conflicts.items())}),
        )
        object.__setattr__(self, "unresolved", tuple(sorted(set(self.unresolved))))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(cell.card_id for cell in self.cells)

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "frame_id": self.frame_id,
            "event_id": self.event_id,
            "cells": [cell.to_record() for cell in self.cells],
            "consensus": dict(self.consensus),
            "conflicts": {key: list(values) for key, values in self.conflicts.items()},
            "unresolved": list(self.unresolved),
            "metadata": dict(self.metadata),
        }

    def merge(self, results: tuple[CardResult, ...]) -> CognitiveTable:
        known = set(self.selected_ids)
        result_map: dict[str, CardResult] = {}
        for result in results:
            if result.card_id not in known:
                raise CognitiveTableError(f"선택되지 않은 card 결과입니다: {result.card_id}")
            if result.card_id in result_map:
                raise CognitiveTableError(f"card 결과가 중복됩니다: {result.card_id}")
            result_map[result.card_id] = result
        values: dict[str, set[str]] = {}
        unresolved = set(self.unresolved)
        for result in result_map.values():
            for key, value in result.claims.items():
                values.setdefault(key, set()).add(value)
            unresolved.update(result.unresolved)
        consensus = {key: next(iter(items)) for key, items in values.items() if len(items) == 1}
        conflicts = {key: tuple(sorted(items)) for key, items in values.items() if len(items) > 1}
        canonical = json.dumps(
            {
                "base": self.to_record(),
                "results": [result.to_record() for result in sorted(results, key=lambda item: item.card_id)],
                "consensus": consensus,
                "conflicts": conflicts,
                "unresolved": sorted(unresolved),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return CognitiveTable(
            id=f"table:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
            frame_id=self.frame_id,
            event_id=self.event_id,
            cells=self.cells,
            consensus=consensus,
            conflicts=conflicts,
            unresolved=tuple(unresolved),
            metadata={**self.metadata, "result_count": len(results), "merged": True},
        )


def build_cognitive_table(frame: CognitiveFrame, route: CardRoute) -> CognitiveTable:
    """Build one structured table request without invoking an executor."""
    available = set(route.event.evidence)
    cells: list[TableCell] = []
    unresolved = {f"unknown_card:{card_id}" for card_id in route.unknown_cards}
    unresolved.update(f"skipped_card:{card_id}:{reason}" for card_id, reason in route.skipped.items())
    for card in route.selected:
        required = set(card.requires)
        supplied = required & available
        missing = required - supplied
        if missing:
            unresolved.update(f"missing_evidence:{card.id}:{requirement}" for requirement in missing)
        cells.append(
            TableCell(
                card_id=card.id,
                status="ABSTAIN" if missing else "READY",
                focus=card.focus,
                anti_focus=card.anti_focus,
                output=card.output,
                evidence_requirements=card.requires,
                supplied_evidence=tuple(supplied),
                missing_evidence=tuple(missing),
            )
        )
    canonical = json.dumps(
        {
            "frame_id": frame.id,
            "route": route.to_record(),
            "cells": [cell.to_record() for cell in cells],
            "unresolved": sorted(unresolved),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CognitiveTable(
        id=f"table:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        frame_id=frame.id,
        event_id=route.event.id,
        cells=tuple(cells),
        consensus={},
        conflicts={},
        unresolved=tuple(unresolved),
        metadata={
            "selected_card_count": len(cells),
            "unknown_card_count": len(route.unknown_cards),
            "llm_invoked": False,
        },
    )


__all__ = ["CardResult", "CognitiveTable", "CognitiveTableError", "TableCell", "build_cognitive_table"]
