"""Immutable data catalog for the Phase 34 preset migration.

The catalog is deliberately narrower than a general plugin system.  It loads
the five existing scaffold presets and five existing guided-detail categories
from local JSON, validates their meaning against the existing artifact-role
allowlist, and returns immutable records.  Missing data has a one-release
builtin fallback, but malformed data is rejected rather than silently hidden.

This module never calls a model, uses the network, writes files, or mutates
Canonical State.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from synapse.core.structure_map import ARTIFACT_ROLE_PATHS

CATALOG_VERSION = 1
PRESET_IDS = ("general", "software", "research", "content", "automation")
DETAIL_CATEGORY_IDS = PRESET_IDS
EXPECTED_PRESET_FILE_COUNTS = {
    "general": 10,
    "software": 12,
    "research": 13,
    "content": 13,
    "automation": 13,
}
EXPECTED_SLOT_COUNTS = {
    "general": 7,
    "software": 2,
    "research": 2,
    "content": 3,
    "automation": 2,
}
PRESET_DATA_FILES = tuple(f"presets/{preset_id}.json" for preset_id in PRESET_IDS)
SLOT_DATA_FILES = tuple(f"slots/{category_id}.json" for category_id in DETAIL_CATEGORY_IDS)
SCHEMA_DATA_FILES = ("schemas/preset_schema.json", "schemas/detail_slot_schema.json")

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SLOT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.]*$")
_FILE_STATUSES = {"EMPTY", "GUIDE", "PROPOSED"}
_ANSWER_MODES = {"user_text", "guided_choice", "llm_draft"}


class PresetCatalogError(ValueError):
    """Raised when preset data is malformed or semantically unsafe."""

    def __init__(self, code: str, message: str, *, path: str | Path | None = None) -> None:
        self.code = code
        self.path = str(path) if path is not None else None
        location = f" [{self.path}]" if self.path else ""
        super().__init__(f"{code}: {message}{location}")


class _MissingCatalogData(FileNotFoundError):
    """Internal signal for the explicit PC-E07 fallback path."""


def _fail(code: str, message: str, path: str | Path | None = None) -> PresetCatalogError:
    return PresetCatalogError(code, message, path=path)


def _text(value: Any, label: str, *, path: str | Path, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        raise _fail("PC-E02", f"{label} must be a string", path)
    result = value.strip()
    if not result:
        raise _fail("PC-E06", f"{label} must not be blank", path)
    if len(result) > limit:
        raise _fail("PC-E02", f"{label} is too long", path)
    return result


def _optional_text(value: Any, label: str, *, path: str | Path, limit: int = 2_000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _fail("PC-E02", f"{label} must be a string", path)
    result = value.strip()
    if len(result) > limit:
        raise _fail("PC-E02", f"{label} is too long", path)
    return result


def _strings(value: Any, label: str, *, path: str | Path, limit: int = 240) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise _fail("PC-E02", f"{label} must be an array of strings", path)
    result = tuple(_text(item, label, path=path, limit=limit) for item in value)
    if len(set(result)) != len(result):
        raise _fail("PC-E02", f"{label} contains duplicate items", path)
    return result


def _mapping(value: Any, label: str, *, path: str | Path) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail("PC-E02", f"{label} must be an object", path)
    return value


def _strict_keys(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    label: str,
    path: str | Path,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _fail("PC-E02", f"{label} has unknown fields: {unknown}", path)
    missing = sorted(required - set(value))
    if missing:
        raise _fail("PC-E02", f"{label} is missing fields: {missing}", path)


def _bool(value: Any, label: str, *, path: str | Path) -> bool:
    if not isinstance(value, bool):
        raise _fail("PC-E02", f"{label} must be a boolean", path)
    return value


def _version(value: Any, *, path: str | Path) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != CATALOG_VERSION:
        raise _fail("PC-E02", f"schema_version must be {CATALOG_VERSION}", path)
    return value


def _safe_relative_path(value: Any, *, path: str | Path) -> str:
    result = _text(value, "file.path", path=path, limit=240)
    if "\\" in result:
        raise _fail("PC-E02", "file.path must use POSIX separators", path)
    candidate = PurePosixPath(result)
    if candidate.is_absolute() or result.startswith("/") or ":" in candidate.parts[0]:
        raise _fail("PC-E02", "file.path must be relative", path)
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise _fail("PC-E02", "file.path contains an unsafe segment", path)
    return result


def _validated_id(value: Any, label: str, *, path: str | Path, pattern: re.Pattern[str]) -> str:
    result = _text(value, label, path=path, limit=160)
    if pattern.fullmatch(result) is None:
        raise _fail("PC-E02", f"{label} has an invalid identifier", path)
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class PresetFileRecord:
    """One immutable file candidate declared by a scaffold preset."""

    path: str
    title: str
    purpose: str
    required: bool = True
    initial_status: str = "EMPTY"

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _safe_relative_path(self.path, path="builtin.file.path"))
        object.__setattr__(self, "title", _text(self.title, "file.title", path="builtin.file.title"))
        object.__setattr__(self, "purpose", _text(self.purpose, "file.purpose", path="builtin.file.purpose"))
        if not isinstance(self.required, bool):
            raise _fail("PC-E02", "file.required must be a boolean", "builtin.file.required")
        if self.initial_status not in _FILE_STATUSES:
            raise _fail("PC-E02", "file.initial_status is not supported", "builtin.file.initial_status")

    def to_record(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "purpose": self.purpose,
            "required": self.required,
            "initial_status": self.initial_status,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PresetRecord:
    """One immutable scaffold preset from the data catalog."""

    id: str
    label: str
    description: str
    tags: tuple[str, ...]
    files: tuple[PresetFileRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _validated_id(self.id, "preset.id", path="builtin.preset.id", pattern=_ID_PATTERN))
        object.__setattr__(self, "label", _text(self.label, "preset.label", path="builtin.preset.label"))
        object.__setattr__(self, "description", _text(self.description, "preset.description", path="builtin.preset.description"))
        tags = tuple(self.tags)
        if not tags:
            raise _fail("PC-E02", "preset.tags must not be empty", "builtin.preset.tags")
        object.__setattr__(self, "tags", tuple(_text(item, "preset.tags", path="builtin.preset.tags", limit=80) for item in tags))
        if len(set(self.tags)) != len(self.tags):
            raise _fail("PC-E02", "preset.tags contains duplicates", "builtin.preset.tags")
        object.__setattr__(self, "files", tuple(self.files))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "tags": list(self.tags),
            "files": [item.to_record() for item in self.files],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SlotLensRecord:
    """Expert lens grafted onto a detail slot (TableCard card structure).

    A slot's own ``purpose``/``title`` says *why the slot exists*; the lens says
    *how an expert fills it* -- the narrow reason, the allowed and forbidden
    moves, the questions to hold, and the shape of the output.  It is the
    concrete anchor a cold-start general slot lacks (Phase 68-73 live runs left a
    thin general slot empty because the model had nothing to aim at).  A lens is
    optional: a slot without one keeps the pre-Phase-79 behaviour byte for byte.
    It is defined in exactly one place (the slot data); parsers, and later
    prompts, only derive from it -- never a second copy.
    """

    purpose: str
    do: tuple[str, ...]
    do_not: tuple[str, ...]
    output_format: tuple[str, ...]
    focus_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", _text(self.purpose, "lens.purpose", path="builtin.lens.purpose"))
        object.__setattr__(self, "do", _strings(self.do, "lens.do", path="builtin.lens.do", limit=1_000))
        object.__setattr__(self, "do_not", _strings(self.do_not, "lens.do_not", path="builtin.lens.do_not", limit=1_000))
        object.__setattr__(self, "output_format", _strings(self.output_format, "lens.output_format", path="builtin.lens.output_format", limit=1_000))
        object.__setattr__(self, "focus_questions", _strings(self.focus_questions, "lens.focus_questions", path="builtin.lens.focus_questions", limit=1_000))
        for label, value in (("lens.do", self.do), ("lens.do_not", self.do_not), ("lens.output_format", self.output_format)):
            if not value:
                raise _fail("PC-E02", f"{label} must have at least one item", f"builtin.{label}")

    def to_record(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "do": list(self.do),
            "do_not": list(self.do_not),
            "focus_questions": list(self.focus_questions),
            "output_format": list(self.output_format),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DetailSlotRecord:
    """Static portion of one guided-detail slot."""

    id: str
    category_id: str
    title: str
    purpose: str
    required: bool
    artifact_role: str
    answer_mode: str
    completion_criteria: str
    question: str
    why_needed: str
    suggestions: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    lens: SlotLensRecord | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _validated_id(self.id, "slot.id", path="builtin.slot.id", pattern=_SLOT_ID_PATTERN))
        object.__setattr__(self, "category_id", _validated_id(self.category_id, "slot.category_id", path="builtin.slot.category_id", pattern=_ID_PATTERN))
        for label, value in (
            ("slot.title", self.title),
            ("slot.purpose", self.purpose),
            ("slot.completion_criteria", self.completion_criteria),
            ("slot.question", self.question),
            ("slot.why_needed", self.why_needed),
        ):
            _text(value, label, path=f"builtin.{label}")
        if not isinstance(self.required, bool):
            raise _fail("PC-E02", "slot.required must be a boolean", "builtin.slot.required")
        if self.artifact_role not in ARTIFACT_ROLE_PATHS:
            raise _fail("PC-E04", f"unknown artifact_role: {self.artifact_role}", "builtin.slot.artifact_role")
        if self.answer_mode not in _ANSWER_MODES:
            raise _fail("PC-E02", f"unsupported slot.answer_mode: {self.answer_mode}", "builtin.slot.answer_mode")
        object.__setattr__(self, "suggestions", tuple(self.suggestions))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        if self.lens is not None and not isinstance(self.lens, SlotLensRecord):
            raise _fail("PC-E02", "slot.lens must be a lens record", "builtin.slot.lens")

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self.id,
            "category_id": self.category_id,
            "title": self.title,
            "purpose": self.purpose,
            "required": self.required,
            "artifact_role": self.artifact_role,
            "answer_mode": self.answer_mode,
            "completion_criteria": self.completion_criteria,
            "question": self.question,
            "why_needed": self.why_needed,
            "suggestions": list(self.suggestions),
            "depends_on": list(self.depends_on),
        }
        if self.lens is not None:
            record["lens"] = self.lens.to_record()
        return record

    def to_spec(self) -> dict[str, Any]:
        """Return the current ``guided_detail`` constructor shape."""

        return {
            "id": self.id,
            "category_id": self.category_id,
            "title": self.title,
            "purpose": self.purpose,
            "required": self.required,
            "artifact_role": self.artifact_role,
            "answer_mode": self.answer_mode,
            "completion_criteria": self.completion_criteria,
            "question": self.question,
            "why_needed": self.why_needed,
            "suggestions": self.suggestions,
            "depends_on": self.depends_on,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DetailCategoryRecord:
    """One immutable category and its ordered static slots."""

    id: str
    label: str
    description: str
    base: bool
    slots: tuple[DetailSlotRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _validated_id(self.id, "category.id", path="builtin.category.id", pattern=_ID_PATTERN))
        object.__setattr__(self, "label", _text(self.label, "category.label", path="builtin.category.label"))
        object.__setattr__(self, "description", _text(self.description, "category.description", path="builtin.category.description"))
        if not isinstance(self.base, bool):
            raise _fail("PC-E02", "category.base must be a boolean", "builtin.category.base")
        object.__setattr__(self, "slots", tuple(self.slots))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "base": self.base,
            "slots": [slot.to_record() for slot in self.slots],
        }

    def to_category_record(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "description": self.description, "base": self.base}


@dataclass(frozen=True, slots=True, kw_only=True)
class PresetCatalog:
    """Immutable catalog plus explicit source/fallback observation."""

    version: int
    presets: tuple[PresetRecord, ...]
    categories: tuple[DetailCategoryRecord, ...]
    source: str = "data"
    fallback_used: bool = False
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        if self.version != CATALOG_VERSION:
            raise _fail("PC-E02", f"catalog version must be {CATALOG_VERSION}", "catalog.version")
        if self.source not in {"data", "builtin"}:
            raise _fail("PC-E02", f"unsupported catalog source: {self.source}", "catalog.source")
        if not isinstance(self.fallback_used, bool):
            raise _fail("PC-E02", "catalog.fallback_used must be a boolean", "catalog.fallback_used")
        if self.fallback_used and not self.fallback_reason:
            raise _fail("PC-E02", "fallback reason must be visible", "catalog.fallback_reason")
        object.__setattr__(self, "presets", tuple(self.presets))
        object.__setattr__(self, "categories", tuple(self.categories))

    @property
    def preset_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.presets)

    @property
    def category_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.categories)

    @property
    def slot_count(self) -> int:
        return sum(len(category.slots) for category in self.categories)

    @property
    def slots_with_lens(self) -> int:
        return sum(1 for category in self.categories for slot in category.slots if slot.lens is not None)

    def preset(self, preset_id: str) -> PresetRecord:
        for preset in self.presets:
            if preset.id == preset_id:
                return preset
        raise KeyError(preset_id)

    def category(self, category_id: str) -> DetailCategoryRecord:
        for category in self.categories:
            if category.id == category_id:
                return category
        raise KeyError(category_id)

    def status(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "preset_count": len(self.presets),
            "category_count": len(self.categories),
            "slot_count": self.slot_count,
            "slots_with_lens": self.slots_with_lens,
        }

    def to_record(self) -> dict[str, Any]:
        return {
            **self.status(),
            "presets": [preset.to_record() for preset in self.presets],
            "categories": [category.to_record() for category in self.categories],
        }


_BUILTIN_BASE_FILES = (
    ("synapse.project.yaml", "Project manifest", "프로젝트 이름·프리셋·초안 버전을 기록합니다.", "GUIDE"),
    ("README.md", "사용 안내", "처음 합의한 목표와 다음 작업 순서를 짧게 적습니다.", "GUIDE"),
    ("00_intake/idea.md", "아이디어 원문", "사용자가 처음 적은 아이디어를 원문 그대로 보존합니다.", "EMPTY"),
    ("01_context/goals.md", "목표", "무엇을 만들고 어떤 결과를 원하는지 적습니다.", "EMPTY"),
    ("01_context/constraints.md", "제약과 경계", "하지 않을 일, 환경, 비용, 안전 조건을 적습니다.", "EMPTY"),
    ("02_model/entities.md", "핵심 대상", "사람·파일·개념·기능 등 핵심 대상을 정의합니다.", "EMPTY"),
    ("02_model/relations.md", "관계", "대상 사이의 연결과 의존성을 적습니다.", "EMPTY"),
    ("03_rules/invariants.md", "정합성 규칙", "항상 지켜야 하는 규칙과 금지사항을 적습니다.", "EMPTY"),
    ("04_work/next_steps.md", "다음 작업", "작게 나눌 다음 작업과 완료 조건을 적습니다.", "EMPTY"),
    ("99_review/open_questions.md", "미해결 질문", "아직 결정하지 않은 점을 숨기지 않고 남깁니다.", "EMPTY"),
)

_BUILTIN_PRESET_DATA = (
    ("general", "일반 프로젝트", "어떤 아이디어든 시작할 수 있는 기본 구조입니다.", ("추천", "범용"), ()),
    (
        "software",
        "소프트웨어 / 앱",
        "기능·인터페이스·검증 단계를 더 잘 보이게 합니다.",
        ("코드", "API"),
        (
            ("02_model/interfaces.md", "인터페이스", "입력·출력과 외부 연결 계약을 적습니다."),
            ("05_validation/tests.md", "검증 계획", "테스트와 실패 조건을 적습니다."),
        ),
    ),
    (
        "research",
        "연구 / 지식",
        "자료의 출처와 주장, 검증 방법을 분리해 관리합니다.",
        ("근거", "분석"),
        (
            ("02_model/sources.md", "자료와 출처", "근거가 되는 자료와 provenance를 기록합니다."),
            ("03_rules/method.md", "방법", "분석 방법과 판단 기준을 적습니다."),
            ("04_work/experiments.md", "실험 / 조사", "반복 가능한 조사와 결과를 적습니다."),
        ),
    ),
    (
        "content",
        "콘텐츠 / 스토리",
        "소설뿐 아니라 영상·게임·기획 콘텐츠에도 쓸 수 있습니다.",
        ("스토리", "기획"),
        (
            ("02_model/subjects.md", "등장 대상", "인물·장소·제품·주제 등 콘텐츠의 대상을 적습니다."),
            ("03_rules/continuity.md", "연속성 규칙", "세계관·톤·브랜드·형식의 일관성 규칙을 적습니다."),
            ("04_work/outline.md", "구성", "장면·챕터·에피소드·제작 순서를 적습니다."),
        ),
    ),
    (
        "automation",
        "데이터 / 자동화",
        "입력·변환·출력과 실행 안전 조건을 먼저 정리합니다.",
        ("데이터", "반복 실행"),
        (
            ("02_model/inputs_outputs.md", "입출력", "데이터 형태와 변환 결과를 적습니다."),
            ("03_rules/safety.md", "안전 규칙", "실행 전 확인·롤백·권한 조건을 적습니다."),
            ("04_work/runs.md", "실행 기록", "실행 단위와 재현 방법을 적습니다."),
        ),
    ),
)

_BUILTIN_CATEGORY_DATA = (
    (
        "general",
        "일반 기본",
        "모든 프로젝트에 필요한 공통 뼈대입니다.",
        True,
        (
            {
                "id": "general.goal",
                "title": "원하는 결과",
                "purpose": "이 프로젝트가 끝났을 때 얻고 싶은 결과를 고정합니다.",
                "required": True,
                "artifact_role": "goals",
                "answer_mode": "user_text",
                "completion_criteria": "검토 가능한 결과와 성공 기준이 한 문단으로 정리됩니다.",
                "question": "이 프로젝트가 끝나면 무엇이 달라져 있어야 하나요?",
                "why_needed": "결과가 없으면 이후 세부 항목의 우선순위를 정할 수 없습니다.",
                "suggestions": ("사용 가능한 첫 결과", "검증 가능한 성공 기준"),
            },
            {
                "id": "general.scope",
                "title": "범위와 하지 않을 일",
                "purpose": "이번 단계에 포함할 것과 나중으로 미룰 것을 분리합니다.",
                "required": True,
                "artifact_role": "constraints",
                "answer_mode": "guided_choice",
                "completion_criteria": "포함 범위와 제외 범위가 각각 한 가지 이상 정해집니다.",
                "question": "이번 단계에서 반드시 다룰 것과 다루지 않을 것은 무엇인가요?",
                "why_needed": "범위를 고정해야 구조가 필요 이상으로 커지지 않습니다.",
                "suggestions": ("작은 첫 범위", "명시적 제외 항목", "시간·도구 제약"),
            },
            {
                "id": "general.premises",
                "title": "가설과 전제",
                "purpose": "아이디어가 성립하기 위해 참이라고 가정하는 것을 명시합니다.",
                "required": True,
                "artifact_role": "premises",
                "answer_mode": "llm_draft",
                "completion_criteria": "검증 가능한 전제와 그것이 깨질 때의 영향이 정리됩니다.",
                "question": "이 아이디어가 성립하려면 무엇이 참이라고 가정해야 하나요?",
                "why_needed": "전제를 드러내야 나중에 그것이 틀렸을 때 무엇이 무너지는지 알 수 있습니다.",
                "suggestions": ("사용자 가정", "기술적 전제", "자원·시간 전제", "외부 의존"),
            },
            {
                "id": "general.entities",
                "title": "핵심 대상과 관계",
                "purpose": "사람·개념·파일·기능 등 프로젝트가 다루는 대상을 정리합니다.",
                "required": True,
                "artifact_role": "entities",
                "answer_mode": "user_text",
                "completion_criteria": "핵심 대상과 서로 영향을 주는 관계가 식별됩니다.",
                "question": "이 프로젝트에서 반드시 등장하거나 움직이는 대상은 무엇인가요?",
                "why_needed": "대상이 정해져야 세부 작업과 영향 범위를 계산할 수 있습니다.",
                "suggestions": ("사람/역할", "개념/규칙", "입력/출력"),
            },
            {
                "id": "general.rules",
                "title": "지켜야 할 규칙",
                "purpose": "항상 유지할 정합성·안전·품질 조건을 정합니다.",
                "required": False,
                "artifact_role": "invariants",
                "answer_mode": "user_text",
                "completion_criteria": "변경 때마다 확인할 규칙이 한 가지 이상 기록됩니다.",
                "question": "내용이나 구현이 바뀌어도 절대 깨지면 안 되는 것은 무엇인가요?",
                "why_needed": "나중에 내용을 보강해도 기존 합의를 잃지 않게 합니다.",
                "suggestions": ("안전 조건", "스타일/톤", "호환성", "금지사항"),
            },
            {
                "id": "general.failure_criteria",
                "title": "실패 기준",
                "purpose": "무엇을 보면 이 방향이 실패했다고 판단할지 미리 정합니다.",
                "required": True,
                "artifact_role": "failure_criteria",
                "answer_mode": "llm_draft",
                "completion_criteria": "관찰 가능한 실패 신호와 그때 취할 대응이 한 가지 이상 적힙니다.",
                "question": "무엇이 관찰되면 이 접근을 멈추거나 되돌려야 하나요?",
                "why_needed": "실패 기준이 없으면 잘못된 방향을 오래 붙들게 됩니다.",
                "suggestions": ("성공 기준의 반대", "관찰 가능한 신호", "중단 조건", "롤백 시점"),
            },
            {
                "id": "general.next_step",
                "title": "첫 작업과 완료 조건",
                "purpose": "전체를 한 번에 채우지 않고 지금 할 한 단위로 줄입니다.",
                "required": True,
                "artifact_role": "next_steps",
                "answer_mode": "guided_choice",
                "completion_criteria": "첫 WorkUnit의 입력·결과·완료 조건이 명확합니다.",
                "question": "지금 가장 작게 끝낼 수 있는 첫 작업은 무엇인가요?",
                "why_needed": "프로젝트를 바로 사용할 수 있는 최소 단위를 만들기 위해 필요합니다.",
                "suggestions": ("원문과 목표 확정", "핵심 대상 목록", "첫 장면/기능/조사 질문"),
            },
        ),
    ),
    (
        "software",
        "소프트웨어 / 앱",
        "기능·인터페이스·검증을 세분화합니다.",
        False,
        (
            {
                "id": "software.interfaces",
                "title": "인터페이스와 입출력",
                "purpose": "사용자·서비스·파일 사이의 입력과 출력을 정의합니다.",
                "required": True,
                "artifact_role": "interfaces",
                "answer_mode": "guided_choice",
                "completion_criteria": "주요 입력·출력과 실패 응답이 한 흐름으로 설명됩니다.",
                "question": "누가 무엇을 넣고, 어떤 결과를 받나요?",
                "why_needed": "기능을 만들기 전에 경계를 고정해야 합니다.",
                "suggestions": ("사용자 화면", "API", "파일/폴더", "실패 응답"),
            },
            {
                "id": "software.validation",
                "title": "검증 기준",
                "purpose": "기능이 완성됐다고 판단할 테스트와 실패 조건을 정합니다.",
                "required": True,
                "artifact_role": "tests",
                "answer_mode": "llm_draft",
                "completion_criteria": "정상·경계·실패 사례가 각각 하나 이상 있습니다.",
                "question": "무엇을 확인하면 이 기능이 제대로 됐다고 말할 수 있나요?",
                "why_needed": "구현 후 판단 기준을 나중에 바꾸지 않기 위해 필요합니다.",
                "suggestions": ("정상 흐름", "경계값", "실패/롤백"),
            },
        ),
    ),
    (
        "research",
        "연구 / 지식",
        "자료·방법·검증 흐름을 세분화합니다.",
        False,
        (
            {
                "id": "research.sources",
                "title": "자료와 근거",
                "purpose": "주장을 뒷받침할 자료와 출처의 신뢰도를 분리합니다.",
                "required": True,
                "artifact_role": "sources",
                "answer_mode": "user_text",
                "completion_criteria": "핵심 주장마다 확인할 자료 유형과 출처 기준이 있습니다.",
                "question": "어떤 자료를 근거로 삼고, 무엇을 믿지 않을 건가요?",
                "why_needed": "근거 없는 가정이 사실처럼 굳는 것을 막습니다.",
                "suggestions": ("1차 자료", "비교 자료", "출처 신뢰도 기준"),
            },
            {
                "id": "research.method",
                "title": "방법과 판단 기준",
                "purpose": "자료를 어떻게 비교·분석하고 결론을 보류할지 정합니다.",
                "required": True,
                "artifact_role": "method",
                "answer_mode": "llm_draft",
                "completion_criteria": "반복 가능한 분석 절차와 중단 조건이 설명됩니다.",
                "question": "자료를 어떤 순서와 기준으로 분석할까요?",
                "why_needed": "결론이 자료 선택에 따라 흔들리지 않게 합니다.",
                "suggestions": ("비교 분석", "분류", "사례 검토", "반증 조건"),
            },
        ),
    ),
    (
        "content",
        "콘텐츠 / 스토리",
        "대상·연속성·구성을 세분화합니다.",
        False,
        (
            {
                "id": "content.subjects",
                "title": "대상·독자와 등장 요소",
                "purpose": "누가 어떤 형식으로 경험할지와, 인물·장소·사물·주제의 역할과 관계를 함께 정합니다.",
                "required": True,
                "artifact_role": "subjects",
                "answer_mode": "user_text",
                "completion_criteria": "대상 독자/사용자와 전달 형식, 핵심 대상의 욕구/기능과 서로의 충돌이 함께 정리됩니다.",
                "question": "누가 어떤 형식으로 경험하며, 이야기나 콘텐츠를 움직이는 핵심 대상은 누구/무엇인가요?",
                "why_needed": "톤·분량·구성 선택과 장면·에피소드·콘텐츠 단위 구성이 모두 대상을 중심으로 달라집니다.",
                "suggestions": ("소설", "영상", "게임", "기획 문서", "혼합 형식", "주요 인물", "대립 대상", "장소", "반복 모티프"),
            },
            {
                "id": "content.continuity",
                "title": "연속성·톤 규칙",
                "purpose": "세계관·톤·시점·표현의 일관성 규칙을 정합니다.",
                "required": False,
                "artifact_role": "continuity",
                "answer_mode": "user_text",
                "completion_criteria": "다음 장면/에피소드에서도 유지할 규칙이 기록됩니다.",
                "question": "다음 장면에서도 반드시 유지할 세계·톤·시점 규칙은 무엇인가요?",
                "why_needed": "나중에 채워도 기존 내용과 충돌하지 않게 합니다.",
                "suggestions": ("시점", "금지 표현", "세계 규칙", "공개 순서"),
            },
            {
                "id": "content.outline",
                "title": "구성·전개 순서",
                "purpose": "장면·챕터·에피소드의 순서와 각 단위의 목적을 정합니다.",
                "required": False,
                "artifact_role": "outline",
                "answer_mode": "llm_draft",
                "completion_criteria": "첫 단위와 다음 단위의 전환 조건이 설명됩니다.",
                "question": "전체를 어떤 장면/에피소드 단위로 나누고, 첫 단위는 어디까지인가요?",
                "why_needed": "완성 전에도 첫 작업부터 사용할 수 있게 합니다.",
                "suggestions": ("3막", "챕터", "에피소드", "장면 카드"),
            },
        ),
    ),
    (
        "automation",
        "데이터 / 자동화",
        "입출력·안전·실행 기록을 세분화합니다.",
        False,
        (
            {
                "id": "automation.inputs_outputs",
                "title": "입력·변환·출력",
                "purpose": "자동화의 경계와 데이터 형태를 분명히 합니다.",
                "required": True,
                "artifact_role": "inputs_outputs",
                "answer_mode": "guided_choice",
                "completion_criteria": "입력 형식·변환 단계·출력 형식이 예시와 함께 있습니다.",
                "question": "어떤 입력을 받아 어떤 변환 뒤 어떤 출력으로 만들까요?",
                "why_needed": "자동화가 예상 밖 데이터를 처리하지 않게 합니다.",
                "suggestions": ("텍스트", "표/CSV", "JSON", "폴더 감시"),
            },
            {
                "id": "automation.safety",
                "title": "실행 안전과 롤백",
                "purpose": "실행 전 승인·백업·롤백 조건을 정합니다.",
                "required": True,
                "artifact_role": "safety",
                "answer_mode": "llm_draft",
                "completion_criteria": "실행 전 확인과 실패 시 되돌리는 방법이 있습니다.",
                "question": "실행 전에 무엇을 확인하고 실패하면 어떻게 되돌릴까요?",
                "why_needed": "반복 실행이 데이터를 훼손하지 않게 합니다.",
                "suggestions": ("미리보기", "백업", "dry-run", "승인 단계"),
            },
        ),
    ),
)


def _builtin_file(data: tuple[str, str, str, str] | tuple[str, str, str]) -> PresetFileRecord:
    path, title, purpose, *status = data
    return PresetFileRecord(
        path=path,
        title=title,
        purpose=purpose,
        initial_status=status[0] if status else "EMPTY",
    )


def _builtin_catalog_records() -> tuple[tuple[PresetRecord, ...], tuple[DetailCategoryRecord, ...]]:
    base_files = tuple(_builtin_file(item) for item in _BUILTIN_BASE_FILES)
    presets = tuple(
        PresetRecord(
            id=preset_id,
            label=label,
            description=description,
            tags=tags,
            files=base_files + tuple(_builtin_file(item) for item in extras),
        )
        for preset_id, label, description, tags, extras in _BUILTIN_PRESET_DATA
    )
    categories = tuple(
        DetailCategoryRecord(
            id=category_id,
            label=label,
            description=description,
            base=base,
            slots=tuple(
                DetailSlotRecord(
                    id=str(item["id"]),
                    category_id=category_id,
                    title=str(item["title"]),
                    purpose=str(item["purpose"]),
                    required=bool(item["required"]),
                    artifact_role=str(item["artifact_role"]),
                    answer_mode=str(item["answer_mode"]),
                    completion_criteria=str(item["completion_criteria"]),
                    question=str(item["question"]),
                    why_needed=str(item["why_needed"]),
                    suggestions=tuple(item.get("suggestions", ())),
                    depends_on=tuple(item.get("depends_on", ())),
                )
                for item in slots
            ),
        )
        for category_id, label, description, base, slots in _BUILTIN_CATEGORY_DATA
    )
    return presets, categories


def _ensure_unique(values: Sequence[str], label: str, *, path: str) -> None:
    if len(set(values)) != len(values):
        raise _fail("PC-E03", f"duplicate {label} id", path)


def _validate_catalog_shape(
    presets: tuple[PresetRecord, ...],
    categories: tuple[DetailCategoryRecord, ...],
    *,
    path: str,
) -> None:
    preset_ids = tuple(item.id for item in presets)
    category_ids = tuple(item.id for item in categories)
    _ensure_unique(preset_ids, "preset", path=path)
    _ensure_unique(category_ids, "category", path=path)
    if preset_ids != PRESET_IDS:
        raise _fail("PC-E02", f"preset order/set must be {PRESET_IDS}", path)
    if category_ids != DETAIL_CATEGORY_IDS:
        raise _fail("PC-E02", f"category order/set must be {DETAIL_CATEGORY_IDS}", path)
    if not categories or categories[0].id != "general" or not categories[0].base:
        raise _fail("PC-E02", "general must be the base category", path)
    if any(category.base for category in categories[1:]):
        raise _fail("PC-E02", "only general may be a base category", path)

    general_files = presets[0].files
    general_file_records = tuple(item.to_record() for item in general_files)
    if len(general_files) != EXPECTED_PRESET_FILE_COUNTS["general"]:
        raise _fail("PC-E02", "general file count changed during Phase A", path)
    allowlisted_paths = set(ARTIFACT_ROLE_PATHS.values())
    for preset in presets:
        expected_count = EXPECTED_PRESET_FILE_COUNTS[preset.id]
        if len(preset.files) != expected_count:
            raise _fail("PC-E02", f"{preset.id} file count is not the Phase A count", path)
        if tuple(item.to_record() for item in preset.files[: len(general_files)]) != general_file_records:
            raise _fail("PC-E02", f"{preset.id} does not preserve the common base files", path)
        paths = tuple(item.path for item in preset.files)
        _ensure_unique(paths, f"{preset.id} file", path=path)
        unknown_paths = sorted(set(paths) - allowlisted_paths)
        if unknown_paths:
            raise _fail("PC-E02", f"{preset.id} declares non-allowlisted paths: {unknown_paths}", path)

    slot_ids: list[str] = []
    for category in categories:
        expected_count = EXPECTED_SLOT_COUNTS[category.id]
        if len(category.slots) != expected_count:
            raise _fail("PC-E02", f"{category.id} slot count is not the Phase A count", path)
        category_roles: list[str] = []
        for slot in category.slots:
            if slot.category_id != category.id:
                raise _fail("PC-E02", f"slot {slot.id} has the wrong category", path)
            if slot.artifact_role not in ARTIFACT_ROLE_PATHS:
                raise _fail("PC-E04", f"unknown artifact_role: {slot.artifact_role}", path)
            category_roles.append(slot.artifact_role)
            slot_ids.append(slot.id)
        if len(set(category_roles)) != len(category_roles):
            raise _fail("PC-E08", f"{category.id} has two slots writing the same artifact_role", path)
    _ensure_unique(slot_ids, "slot", path=path)
    known_slots = set(slot_ids)
    for category in categories:
        for slot in category.slots:
            unknown_dependencies = sorted(set(slot.depends_on) - known_slots)
            if unknown_dependencies:
                raise _fail("PC-E05", f"slot {slot.id} has unknown dependencies: {unknown_dependencies}", path)


def _catalog(
    presets: tuple[PresetRecord, ...],
    categories: tuple[DetailCategoryRecord, ...],
    *,
    source: str,
    fallback_used: bool,
    fallback_reason: str | None,
    path: str,
) -> PresetCatalog:
    _validate_catalog_shape(presets, categories, path=path)
    return PresetCatalog(
        version=CATALOG_VERSION,
        presets=presets,
        categories=categories,
        source=source,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )


def builtin_preset_catalog() -> PresetCatalog:
    """Return the immutable one-release builtin catalog without fallback status."""

    presets, categories = _builtin_catalog_records()
    return _catalog(
        presets,
        categories,
        source="builtin",
        fallback_used=False,
        fallback_reason=None,
        path="builtin",
    )


def _fallback_catalog(reason: str) -> PresetCatalog:
    presets, categories = _builtin_catalog_records()
    return _catalog(
        presets,
        categories,
        source="builtin",
        fallback_used=True,
        fallback_reason=reason,
        path="builtin-fallback",
    )


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise _MissingCatalogData(str(path))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _fail("PC-E01", f"invalid JSON: {exc.msg}", path) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail("PC-E01", f"catalog file cannot be read: {exc}", path) from exc


def _read_schema(path: Path) -> None:
    payload = _read_json(path)
    _mapping(payload, "schema", path=path)


def _parse_file(payload: Any, *, path: str) -> PresetFileRecord:
    value = _mapping(payload, "preset file", path=path)
    _strict_keys(
        value,
        allowed={"path", "title", "purpose", "required", "initial_status"},
        required={"path", "title", "purpose", "required", "initial_status"},
        label="preset file",
        path=path,
    )
    file_path = _safe_relative_path(value["path"], path=f"{path}.path")
    title = _text(value["title"], "file.title", path=f"{path}.title")
    purpose = _text(value["purpose"], "file.purpose", path=f"{path}.purpose")
    required = _bool(value["required"], "file.required", path=f"{path}.required")
    initial_status = _text(value["initial_status"], "file.initial_status", path=f"{path}.initial_status")
    if initial_status not in _FILE_STATUSES:
        raise _fail("PC-E02", f"unsupported file.initial_status: {initial_status}", f"{path}.initial_status")
    return PresetFileRecord(path=file_path, title=title, purpose=purpose, required=required, initial_status=initial_status)


def _parse_preset(payload: Any, *, path: str) -> PresetRecord:
    value = _mapping(payload, "preset", path=path)
    _strict_keys(
        value,
        allowed={"schema_version", "id", "label", "description", "tags", "files"},
        required={"schema_version", "id", "label", "description", "tags", "files"},
        label="preset",
        path=path,
    )
    _version(value["schema_version"], path=f"{path}.schema_version")
    preset_id = _validated_id(value["id"], "preset.id", path=f"{path}.id", pattern=_ID_PATTERN)
    label = _text(value["label"], "preset.label", path=f"{path}.label")
    description = _text(value["description"], "preset.description", path=f"{path}.description")
    tags = _strings(value["tags"], "preset.tags", path=f"{path}.tags", limit=80)
    files_raw = value["files"]
    if not isinstance(files_raw, list) or not files_raw:
        raise _fail("PC-E02", "preset.files must be a non-empty array", f"{path}.files")
    files = tuple(_parse_file(item, path=f"{path}.files[{index}]") for index, item in enumerate(files_raw))
    return PresetRecord(id=preset_id, label=label, description=description, tags=tags, files=files)


def _parse_lens(payload: Any, *, path: str) -> SlotLensRecord:
    value = _mapping(payload, "slot lens", path=path)
    _strict_keys(
        value,
        allowed={"purpose", "do", "do_not", "focus_questions", "output_format"},
        required={"purpose", "do", "do_not", "output_format"},
        label="slot lens",
        path=path,
    )
    purpose = _text(value["purpose"], "lens.purpose", path=f"{path}.purpose")
    do = _strings(value["do"], "lens.do", path=f"{path}.do", limit=1_000)
    do_not = _strings(value["do_not"], "lens.do_not", path=f"{path}.do_not", limit=1_000)
    output_format = _strings(value["output_format"], "lens.output_format", path=f"{path}.output_format", limit=1_000)
    focus_questions = _strings(value.get("focus_questions", []), "lens.focus_questions", path=f"{path}.focus_questions", limit=1_000)
    return SlotLensRecord(
        purpose=purpose,
        do=do,
        do_not=do_not,
        output_format=output_format,
        focus_questions=focus_questions,
    )


def _parse_slot(payload: Any, *, category_id: str, path: str) -> DetailSlotRecord:
    value = _mapping(payload, "detail slot", path=path)
    _strict_keys(
        value,
        allowed={
            "id",
            "category_id",
            "title",
            "purpose",
            "required",
            "artifact_role",
            "answer_mode",
            "completion_criteria",
            "question",
            "why_needed",
            "suggestions",
            "depends_on",
            "lens",
        },
        required={
            "id",
            "category_id",
            "title",
            "purpose",
            "required",
            "artifact_role",
            "answer_mode",
            "completion_criteria",
            "question",
            "why_needed",
        },
        label="detail slot",
        path=path,
    )
    slot_id = _validated_id(value["id"], "slot.id", path=f"{path}.id", pattern=_SLOT_ID_PATTERN)
    declared_category = _validated_id(value["category_id"], "slot.category_id", path=f"{path}.category_id", pattern=_ID_PATTERN)
    if declared_category != category_id:
        raise _fail("PC-E02", f"slot category must be {category_id}", f"{path}.category_id")
    title = _text(value["title"], "slot.title", path=f"{path}.title")
    purpose = _text(value["purpose"], "slot.purpose", path=f"{path}.purpose")
    required = _bool(value["required"], "slot.required", path=f"{path}.required")
    artifact_role = _text(value["artifact_role"], "slot.artifact_role", path=f"{path}.artifact_role", limit=80)
    if artifact_role not in ARTIFACT_ROLE_PATHS:
        raise _fail("PC-E04", f"unknown artifact_role: {artifact_role}", f"{path}.artifact_role")
    answer_mode = _text(value["answer_mode"], "slot.answer_mode", path=f"{path}.answer_mode", limit=80)
    if answer_mode not in _ANSWER_MODES:
        raise _fail("PC-E02", f"unsupported slot.answer_mode: {answer_mode}", f"{path}.answer_mode")
    completion_criteria = _text(value["completion_criteria"], "slot.completion_criteria", path=f"{path}.completion_criteria")
    question = _text(value["question"], "slot.question", path=f"{path}.question")
    why_needed = _text(value["why_needed"], "slot.why_needed", path=f"{path}.why_needed")
    suggestions = _strings(value.get("suggestions", []), "slot.suggestions", path=f"{path}.suggestions", limit=240)
    depends_on = _strings(value.get("depends_on", []), "slot.depends_on", path=f"{path}.depends_on", limit=160)
    lens = _parse_lens(value["lens"], path=f"{path}.lens") if "lens" in value else None
    return DetailSlotRecord(
        id=slot_id,
        category_id=declared_category,
        title=title,
        purpose=purpose,
        required=required,
        artifact_role=artifact_role,
        answer_mode=answer_mode,
        completion_criteria=completion_criteria,
        question=question,
        why_needed=why_needed,
        suggestions=suggestions,
        depends_on=depends_on,
        lens=lens,
    )


def _parse_category(payload: Any, *, path: str) -> DetailCategoryRecord:
    value = _mapping(payload, "detail category", path=path)
    _strict_keys(
        value,
        allowed={"schema_version", "category_id", "label", "description", "base", "slots"},
        required={"schema_version", "category_id", "label", "description", "base", "slots"},
        label="detail category",
        path=path,
    )
    _version(value["schema_version"], path=f"{path}.schema_version")
    category_id = _validated_id(value["category_id"], "category.id", path=f"{path}.category_id", pattern=_ID_PATTERN)
    label = _text(value["label"], "category.label", path=f"{path}.label")
    description = _text(value["description"], "category.description", path=f"{path}.description")
    base = _bool(value["base"], "category.base", path=f"{path}.base")
    slots_raw = value["slots"]
    if not isinstance(slots_raw, list) or not slots_raw:
        raise _fail("PC-E02", "category.slots must be a non-empty array", f"{path}.slots")
    slots = tuple(_parse_slot(item, category_id=category_id, path=f"{path}.slots[{index}]") for index, item in enumerate(slots_raw))
    return DetailCategoryRecord(id=category_id, label=label, description=description, base=base, slots=slots)


def load_preset_catalog(data_root: str | Path | None = None) -> PresetCatalog:
    """Load the local data catalog or explicitly report the builtin fallback."""

    root = Path(data_root) if data_root is not None else Path(__file__).resolve().parents[2] / "domain_packs" / "_presets"
    try:
        for relative in SCHEMA_DATA_FILES:
            _read_schema(root / relative)
        preset_payloads = [(relative, _read_json(root / relative)) for relative in PRESET_DATA_FILES]
        category_payloads = [(relative, _read_json(root / relative)) for relative in SLOT_DATA_FILES]
    except _MissingCatalogData as exc:
        return _fallback_catalog(f"PC-E07: catalog data missing ({exc.args[0]})")

    presets = tuple(_parse_preset(payload, path=relative) for relative, payload in preset_payloads)
    categories = tuple(_parse_category(payload, path=relative) for relative, payload in category_payloads)
    return _catalog(
        presets,
        categories,
        source="data",
        fallback_used=False,
        fallback_reason=None,
        path=str(root),
    )


def get_preset_catalog_status(data_root: str | Path | None = None) -> dict[str, Any]:
    """Return an explicit, JSON-friendly observation of the selected source."""

    return load_preset_catalog(data_root).status()


__all__ = [
    "CATALOG_VERSION",
    "DETAIL_CATEGORY_IDS",
    "EXPECTED_PRESET_FILE_COUNTS",
    "EXPECTED_SLOT_COUNTS",
    "PRESET_IDS",
    "DetailCategoryRecord",
    "DetailSlotRecord",
    "PresetCatalog",
    "PresetCatalogError",
    "PresetFileRecord",
    "PresetRecord",
    "SlotLensRecord",
    "builtin_preset_catalog",
    "get_preset_catalog_status",
    "load_preset_catalog",
]
