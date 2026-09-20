"""Domain-neutral idea-to-scaffold planning.

The scaffold planner is intentionally a proposal boundary.  It turns a small
idea seed and a selected preset into a deterministic folder/file checklist;
it never creates files, calls a model, or mutates Canonical State.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from synapse.core.preset_catalog import (
    PresetCatalog,
    PresetFileRecord,
    PresetRecord,
    load_preset_catalog,
)


class ScaffoldError(ValueError):
    """Raised when an idea-to-scaffold request is not safe or complete."""


@dataclass(frozen=True, slots=True)
class ScaffoldFile:
    path: str
    title: str
    purpose: str
    required: bool = True
    initial_status: str = "EMPTY"

    def __post_init__(self) -> None:
        if not self.path.strip() or self.path.startswith("/") or ".." in self.path.split("/"):
            raise ScaffoldError(f"안전하지 않은 scaffold 경로입니다: {self.path}")
        if not self.title.strip() or not self.purpose.strip():
            raise ScaffoldError("ScaffoldFile title과 purpose가 필요합니다.")
        if self.initial_status not in {"EMPTY", "GUIDE", "PROPOSED"}:
            raise ScaffoldError(f"지원하지 않는 초기 파일 상태입니다: {self.initial_status}")

    def to_record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "title": self.title,
            "purpose": self.purpose,
            "required": self.required,
            "status": self.initial_status,
        }


@dataclass(frozen=True, slots=True)
class ScaffoldStep:
    id: str
    title: str
    instruction: str
    file_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.title.strip() or not self.instruction.strip():
            raise ScaffoldError("ScaffoldStep 식별자와 안내가 필요합니다.")
        object.__setattr__(self, "file_paths", tuple(dict.fromkeys(self.file_paths)))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "instruction": self.instruction,
            "file_paths": list(self.file_paths),
        }


@dataclass(frozen=True, slots=True)
class ScaffoldPreset:
    id: str
    label: str
    description: str
    extra_files: tuple[ScaffoldFile, ...] = ()
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.label.strip() or not self.description.strip():
            raise ScaffoldError("ScaffoldPreset 기본 설명이 비어 있습니다.")
        object.__setattr__(self, "extra_files", tuple(self.extra_files))
        object.__setattr__(self, "tags", tuple(dict.fromkeys(self.tags)))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class ScaffoldPlan:
    id: str
    project_name: str
    project_slug: str
    preset: ScaffoldPreset
    idea: str
    files: tuple[ScaffoldFile, ...]
    steps: tuple[ScaffoldStep, ...]
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.project_name.strip() or not self.project_slug.strip():
            raise ScaffoldError("ScaffoldPlan 식별자가 비어 있습니다.")
        if not self.idea.strip() or not self.files or not self.steps:
            raise ScaffoldError("ScaffoldPlan에는 idea, files, steps가 필요합니다.")
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ScaffoldError("ScaffoldPlan 파일 경로가 중복됩니다.")
        known = set(paths)
        missing = sorted({path for step in self.steps for path in step.file_paths if path not in known})
        if missing:
            raise ScaffoldError(f"ScaffoldStep가 알 수 없는 파일을 참조합니다: {missing}")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def required_count(self) -> int:
        return sum(item.required for item in self.files)

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "project_name": self.project_name,
            "project_slug": self.project_slug,
            "preset": self.preset.to_record(),
            "idea": self.idea,
            "files": [item.to_record() for item in self.files],
            "steps": [step.to_record() for step in self.steps],
            "progress": {"completed": 0, "total": len(self.files), "percent": 0},
            "metadata": dict(self.metadata),
            "canonical_mutation": False,
        }


def _file_from_record(record: PresetFileRecord) -> ScaffoldFile:
    return ScaffoldFile(
        record.path,
        record.title,
        record.purpose,
        required=record.required,
        initial_status=record.initial_status,
    )


def _preset_from_record(record: PresetRecord, base_count: int) -> ScaffoldPreset:
    return ScaffoldPreset(
        record.id,
        record.label,
        record.description,
        extra_files=tuple(_file_from_record(item) for item in record.files[base_count:]),
        tags=record.tags,
    )


def _catalog_views() -> tuple[PresetCatalog, tuple[ScaffoldPreset, ...]]:
    catalog = load_preset_catalog()
    base_count = len(catalog.preset("general").files)
    presets = tuple(_preset_from_record(record, base_count) for record in catalog.presets)
    return catalog, presets


def list_scaffold_presets() -> tuple[ScaffoldPreset, ...]:
    """Return the supported domain-neutral presets in stable order."""

    return _catalog_views()[1]


def _slug(value: str) -> str:
    normalized = re.sub(r"[^0-9a-z가-힣]+", "-", value.strip().lower()).strip("-")
    return normalized or "synapse-project"


# A run of these is how people draw a line, not how they name a thing. A line
# made only of them, or one fenced by them at both ends, is a separator carrying
# a label -- "--- README.md ---" -- and the label is the file it came from.
_SEPARATOR_RUN = r"[-=*_~]{3,}"
_SEPARATOR_LINE = re.compile(rf"^\s*(?:{_SEPARATOR_RUN}\s*.*?\s*{_SEPARATOR_RUN}|{_SEPARATOR_RUN})\s*$")
_HAS_WORD = re.compile(r"[^\W_]")
_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*$")
# A line that closes the way a sentence closes. Used only to decide whether the
# author had already started writing before the first heading appeared.
_PROSE_LINE = re.compile(r"(?:[.!?。！？]|[다요죠까](?:\.)?)\s*$")


def _name_from_idea(idea: str) -> str:
    """The line that claims to be a title, not the line that happens to be first.

    An idea written for this tool opens with its own name, so the first line is
    the title and nothing here changes that. Material that was already underway
    somewhere else opens with whatever the person pasted first -- a separator, a
    file banner, a date, a greeting, a bullet, an opening code fence -- and its
    real title is a heading a line or two below. Taking the first line
    regardless made "--- README.md ---" the project name for a folder that said
    "# 중고 시세 추적기" immediately after it.

    So a heading wins, but only while nothing has been *written* yet: once a
    line closes like a sentence, the author has started, and a heading after
    that is a section of their text rather than the name of it.
    """
    scrap = ""
    for line in idea.splitlines():
        if not line.strip() or _SEPARATOR_LINE.match(line):
            continue
        heading = _HEADING_LINE.match(line)
        if heading:
            return heading.group("text")[:48].strip()
        candidate = line.strip(" #\t").strip()[:48].strip()
        if not candidate or not _HAS_WORD.search(candidate):
            continue
        if _PROSE_LINE.search(line.strip()):
            return candidate
        scrap = scrap or candidate
    if scrap:
        return scrap
    # Every line was a separator, so the label inside one is all there is.
    for line in idea.splitlines():
        candidate = line.strip().strip("-=*_~ \t#").strip()[:48].strip()
        if candidate and _HAS_WORD.search(candidate):
            return candidate
    return "새 Synapse 프로젝트"


def build_scaffold_plan(
    idea: str,
    *,
    preset_id: str = "general",
    project_name: str = "",
) -> ScaffoldPlan:
    """Build a deterministic scaffold proposal without writing to disk."""

    seed = str(idea).strip()
    if len(seed) < 3:
        raise ScaffoldError("아이디어를 세 글자 이상 적어 주세요.")
    catalog, presets = _catalog_views()
    preset_record = next((item for item in catalog.presets if item.id == str(preset_id).strip()), None)
    if preset_record is None:
        raise ScaffoldError(f"지원하지 않는 프리셋입니다: {preset_id}")
    preset = next(item for item in presets if item.id == preset_record.id)
    name = str(project_name).strip() or _name_from_idea(seed)
    files = tuple(_file_from_record(item) for item in preset_record.files)
    steps = (
        ScaffoldStep("idea", "아이디어 붙잡기", "아이디어 원문과 목표를 먼저 채웁니다.", ("00_intake/idea.md", "01_context/goals.md")),
        ScaffoldStep("context", "범위 정하기", "제약과 하지 않을 일을 적어 과도한 확장을 막습니다.", ("01_context/constraints.md",)),
        ScaffoldStep("model", "대상과 관계", "핵심 대상과 연결을 적습니다.", tuple(item.path for item in files if item.path.startswith("02_model/"))),
        ScaffoldStep("rules", "정합성 규칙", "Synapse가 이후 변경 시 지켜야 할 규칙을 적습니다.", tuple(item.path for item in files if item.path.startswith("03_rules/"))),
        ScaffoldStep("work", "첫 작업 고르기", "가장 작은 다음 작업과 미해결 질문을 남깁니다.", ("04_work/next_steps.md", "99_review/open_questions.md")),
    )
    canonical = json.dumps(
        {"name": name, "slug": _slug(name), "preset": preset.id, "idea": seed, "files": [item.to_record() for item in files]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ScaffoldPlan(
        id=f"scaffold:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        project_name=name,
        project_slug=_slug(name),
        preset=preset,
        idea=seed,
        files=files,
        steps=steps,
        metadata={"version": 1, "preset_count": len(presets), "llm_invoked": False},
    )


__all__ = [
    "ScaffoldError",
    "ScaffoldFile",
    "ScaffoldPlan",
    "ScaffoldPreset",
    "ScaffoldStep",
    "build_scaffold_plan",
    "list_scaffold_presets",
]
