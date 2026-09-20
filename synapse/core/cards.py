"""Deterministic TableCard registry and event-driven router."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


class CardRoutingError(ValueError):
    """Raised when a card catalog or route request is structurally invalid."""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted({_text(item) for item in value if _text(item)}))


@dataclass(frozen=True, slots=True)
class CardSpec:
    id: str
    domain: str
    title: str
    mode: str
    triggers: tuple[str, ...]
    requires: tuple[str, ...]
    focus: tuple[str, ...]
    anti_focus: tuple[str, ...]
    output: tuple[str, ...]
    abstain_if: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id.strip() or "/" not in self.id or not self.domain.strip():
            raise CardRoutingError("CardSpec id/domain이 필요합니다.")
        if not self.title.strip() or not self.mode.strip():
            raise CardRoutingError("CardSpec title/mode가 필요합니다.")
        for name in ("triggers", "requires", "focus", "anti_focus", "output", "abstain_if"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "domain": self.domain,
            "title": self.title,
            "mode": self.mode,
            "triggers": list(self.triggers),
            "requires": list(self.requires),
            "focus": list(self.focus),
            "anti_focus": list(self.anti_focus),
            "output": list(self.output),
            "abstain_if": list(self.abstain_if),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RouterEvent:
    id: str
    kind: str
    signals: tuple[str, ...] = ()
    focus: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.kind.strip():
            raise CardRoutingError("RouterEvent id/kind가 필요합니다.")
        for name in ("signals", "focus", "evidence"):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset((self.kind, *self.signals, *self.focus))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "signals": list(self.signals),
            "focus": list(self.focus),
            "evidence": list(self.evidence),
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class CardRoute:
    event: RouterEvent
    selected: tuple[CardSpec, ...]
    skipped: Mapping[str, str]
    unknown_cards: tuple[str, ...]
    evidence_requirements: tuple[str, ...]
    anti_focus: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected", tuple(sorted(self.selected, key=lambda card: card.id)))
        object.__setattr__(self, "skipped", MappingProxyType(dict(sorted(self.skipped.items()))))
        object.__setattr__(self, "unknown_cards", tuple(sorted(set(self.unknown_cards))))
        object.__setattr__(self, "evidence_requirements", tuple(sorted(set(self.evidence_requirements))))
        object.__setattr__(self, "anti_focus", tuple(sorted(set(self.anti_focus))))

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(card.id for card in self.selected)

    def to_record(self) -> dict[str, object]:
        return {
            "event": self.event.to_record(),
            "selected_ids": list(self.selected_ids),
            "selected": [card.to_record() for card in self.selected],
            "skipped": dict(self.skipped),
            "unknown_cards": list(self.unknown_cards),
            "evidence_requirements": list(self.evidence_requirements),
            "anti_focus": list(self.anti_focus),
        }


_DEFAULT_TRIGGERS: dict[str, frozenset[str]] = {
    "core/intake_boundary": frozenset({"bundle_intake", "manual_review", "unknown"}),
    "core/decompose": frozenset({"bundle_intake", "project_changed", "manual_review"}),
    "core/evidence": frozenset({"bundle_intake", "file_modified", "card_results", "manual_review"}),
    "core/negative_case": frozenset({"test_failed", "conflict", "project_changed"}),
    "core/synthesis_conflict": frozenset({"card_results", "conflict"}),
    "core/decision_options": frozenset({"conflict", "review_required", "manual_review"}),
}


class CardRouter:
    """Read-only catalog loader and deterministic event router."""

    def __init__(self, table_card_root: str | Path) -> None:
        root = Path(table_card_root).expanduser().resolve()
        data_root = (root / "data").resolve()
        cards_root = (data_root / "cards").resolve()
        if not data_root.is_dir() or not cards_root.is_dir():
            raise CardRoutingError(f"TableCard data/cards 디렉터리가 없습니다: {data_root}")
        try:
            data_root.relative_to(root)
            cards_root.relative_to(data_root)
        except ValueError as exc:
            raise CardRoutingError("TableCard catalog 경계가 잘못되었습니다.") from exc
        self.root = root
        self.data_root = data_root
        self.cards_root = cards_root
        self.profiles_root = data_root / "profiles"

    def _safe_path(self, base: Path, relative: str) -> Path:
        normalized = relative.replace("\\", "/").strip()
        path = (base / Path(*[part for part in normalized.split("/") if part])).resolve()
        try:
            path.relative_to(self.data_root)
        except ValueError as exc:
            raise CardRoutingError(f"catalog 경계를 벗어난 path입니다: {relative}") from exc
        if path.suffix.casefold() != ".json":
            raise CardRoutingError(f"JSON 카드 path가 아닙니다: {relative}")
        return path

    @staticmethod
    def _read_json(path: Path) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CardRoutingError(f"card JSON을 읽을 수 없습니다: {path}") from exc
        if not isinstance(value, dict):
            raise CardRoutingError(f"card JSON 객체가 아닙니다: {path}")
        return MappingProxyType(dict(value))

    def card_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                f"{path.parent.name}/{path.stem}"
                for path in self.cards_root.rglob("*.json")
                if path.is_file()
            )
        )

    def profile_card_ids(self, profile_id: str) -> tuple[str, ...]:
        normalized = profile_id.strip()
        if not normalized or Path(normalized).name != normalized:
            raise CardRoutingError(f"잘못된 profile id입니다: {profile_id}")
        path = self._safe_path(self.profiles_root, f"{normalized}.json")
        if not path.is_file():
            raise CardRoutingError(f"profile을 찾을 수 없습니다: {profile_id}")
        payload = self._read_json(path)
        refs = payload.get("cards")
        if not isinstance(refs, list):
            raise CardRoutingError(f"profile cards가 잘못되었습니다: {profile_id}")
        return tuple(_text(ref) for ref in refs if _text(ref))

    def load_card(self, card_id: str) -> CardSpec:
        normalized = card_id.strip().replace("\\", "/")
        parts = tuple(part for part in normalized.split("/") if part)
        if len(parts) != 2 or any(part in {".", ".."} for part in parts):
            raise CardRoutingError(f"잘못된 card id입니다: {card_id}")
        domain, local_id = parts
        path = self._safe_path(self.cards_root / domain, f"{local_id}.json")
        if not path.is_file():
            raise CardRoutingError(f"card를 찾을 수 없습니다: {card_id}")
        payload = self._read_json(path)
        if _text(payload.get("id")) != local_id:
            raise CardRoutingError(f"card id가 경로와 다릅니다: {card_id}")
        return CardSpec(
            id=f"{domain}/{local_id}",
            domain=domain,
            title=_text(payload.get("title")) or local_id,
            mode=_text(payload.get("mode")) or "review",
            triggers=_strings(payload.get("trigger", payload.get("triggers")))
            or _DEFAULT_TRIGGERS.get(f"{domain}/{local_id}", ()),
            requires=_strings(payload.get("requires", payload.get("evidence_requirements"))),
            focus=_strings(payload.get("focus", payload.get("focus_questions"))),
            anti_focus=_strings(payload.get("anti_focus", payload.get("do_not"))),
            output=_strings(payload.get("output", payload.get("output_format"))),
            abstain_if=_strings(payload.get("abstain_if", payload.get("abstain"))),
        )

    def route(
        self,
        event: RouterEvent,
        *,
        profile_id: str | None = None,
        card_ids: tuple[str, ...] | None = None,
    ) -> CardRoute:
        refs = card_ids if card_ids is not None else (
            self.profile_card_ids(profile_id) if profile_id else self.card_ids()
        )
        selected: list[CardSpec] = []
        skipped: dict[str, str] = {}
        unknown: list[str] = []
        tokens = event.tokens
        for card_id in sorted(set(refs)):
            try:
                card = self.load_card(card_id)
            except CardRoutingError:
                unknown.append(card_id)
                continue
            abstain_tokens = set(card.abstain_if) & tokens
            if abstain_tokens:
                skipped[card.id] = f"abstain:{','.join(sorted(abstain_tokens))}"
                continue
            explicit_focus = set(event.focus)
            if explicit_focus and card.id not in explicit_focus and card.domain not in explicit_focus:
                skipped[card.id] = "outside_focus"
                continue
            if not (set(card.triggers) & tokens):
                skipped[card.id] = "trigger_mismatch"
                continue
            selected.append(card)
        return CardRoute(
            event=event,
            selected=tuple(selected),
            skipped=skipped,
            unknown_cards=tuple(unknown),
            evidence_requirements=tuple(
                requirement for card in selected for requirement in card.requires
            ),
            anti_focus=tuple(focus for card in selected for focus in card.anti_focus),
        )


__all__ = ["CardRoute", "CardRouter", "CardRoutingError", "CardSpec", "RouterEvent"]
