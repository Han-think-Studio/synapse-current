"""Safe workspace inspection and file Proposal boundaries.

The workspace layer is deliberately deterministic and domain-neutral.  It can
inspect a scaffold, explain which files are affected by an edit, and create a
Proposal for one file.  Nothing is written until the caller supplies an
explicit approval token to :func:`apply_file_proposal`.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from synapse.core.scaffold import ScaffoldPlan


class WorkspaceError(ValueError):
    """Raised when a workspace request is invalid or cannot be applied safely."""


_MAX_READ_BYTES = 512 * 1024
_STATUSES = {"MISSING", "EMPTY", "READY", "BINARY", "TOO_LARGE", "UNSAFE"}


def _normalise_relative_path(value: str) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or ":" in raw:
        raise WorkspaceError(f"안전하지 않은 workspace 파일 경로입니다: {value}")
    parts = tuple(part for part in raw.split("/") if part not in {""})
    if not parts or any(part in {".", ".."} for part in parts):
        raise WorkspaceError(f"안전하지 않은 workspace 파일 경로입니다: {value}")
    return "/".join(parts)


def _resolve_workspace(value: str | Path | None) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise WorkspaceError("workspace 폴더를 지정해 주세요.")
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"workspace 경로를 읽지 못했습니다: {exc}") from exc


def _safe_target(root: Path, relative: str) -> Path:
    path = (root / Path(*_normalise_relative_path(relative).split("/"))).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"workspace 경계를 벗어난 파일입니다: {relative}") from exc
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _starter_content(plan: ScaffoldPlan, path: str, title: str, purpose: str) -> str:
    """Return a small deterministic writing guide, never an inferred answer."""

    if path == "synapse.project.yaml":
        return (
            "# Synapse project manifest\n"
            f"name: {plan.project_name}\n"
            f"slug: {plan.project_slug}\n"
            f"preset: {plan.preset.id}\n"
            f"scaffold_id: {plan.id}\n"
            "status: PROPOSED\n"
        )
    if path == "README.md":
        return f"# {plan.project_name}\n\n{purpose}\n\n## 시작점\n\n{plan.idea}\n"
    return f"# {title}\n\n> {purpose}\n\n## 기록\n\n"


_PLACEHOLDER_LINE = re.compile(
    r"^(?:[-*+]\s*)?(?:\[[ xX]\]\s*)?(?:todo|tbd|fixme|placeholder|"
    r"fill in|write here|to be filled|lorem ipsum|내용 입력|여기에 작성|"
    r"작성 예정|추가 예정|미정|미작성|아직 없음)(?:\s*[:：.!?…-].*)?$",
    re.IGNORECASE,
)


def has_substantive_workspace_content(content: object, *, scaffold_purpose: str) -> bool:
    """Detect authored payload and reject empty Markdown leaf sections."""

    if not isinstance(content, str):
        return False
    headings: list[dict[str, object]] = []
    heading_stack: list[dict[str, object]] = []
    has_payload = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"(?:-{3,}|_{3,}|\*{3,}|={3,}|`{3,}|~{3,})", stripped):
            continue
        if re.fullmatch(r"(?:\|\s*:?-{3,}:?\s*)+\|?", stripped):
            continue
        heading = re.match(r"^(#{1,6})(?:\s+|$)(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            while heading_stack and int(heading_stack[-1]["level"]) >= level:
                heading_stack.pop()
            entry: dict[str, object] = {"level": level, "content": False, "child": False}
            if heading_stack:
                heading_stack[-1]["child"] = True
            headings.append(entry)
            heading_stack.append(entry)
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        if _PLACEHOLDER_LINE.fullmatch(stripped):
            continue
        if re.fullmatch(r"(?:[-*+]\s*)?(?:\[\s*[xX]?\s*\])?", stripped):
            continue
        if re.fullmatch(r"[|:\-\s]+", stripped):
            continue
        if re.match(r"^(?:[-*+]\s*)?[\w.-]+\s*:\s*(?:\"\"|''|null|none)?\s*$", stripped, re.IGNORECASE):
            continue
        if stripped.startswith(">") and scaffold_purpose.strip():
            quoted_text = stripped[1:].lstrip()
            if quoted_text == scaffold_purpose.strip():
                continue
        has_payload = True
        if heading_stack:
            heading_stack[-1]["content"] = True
    if not has_payload:
        return False
    leaf_sections = [heading for heading in headings if not heading["child"]]
    return all(bool(heading["content"]) for heading in leaf_sections)


@dataclass(frozen=True, slots=True)
class WorkspaceFileState:
    path: str
    title: str
    purpose: str
    required: bool
    status: str
    exists: bool
    size: int
    sha256: str | None
    content: str
    suggested_content: str
    detail: str = ""
    substantive: bool = False

    def __post_init__(self) -> None:
        if not self.path.strip() or self.status not in _STATUSES:
            raise WorkspaceError("WorkspaceFileState path/status가 잘못되었습니다.")
        if self.size < 0:
            raise WorkspaceError("WorkspaceFileState size는 0 이상이어야 합니다.")

    def to_record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "title": self.title,
            "purpose": self.purpose,
            "required": self.required,
            "status": self.status,
            "exists": self.exists,
            "size": self.size,
            "sha256": self.sha256,
            "content": self.content,
            "suggested_content": self.suggested_content,
            "detail": self.detail,
            "substantive": self.substantive,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    workspace_path: str
    status: str
    exists: bool
    is_directory: bool
    writable: bool
    plan_id: str
    files: tuple[WorkspaceFileState, ...]
    progress: Mapping[str, int]
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"MISSING", "READY", "INVALID"}:
            raise WorkspaceError(f"지원하지 않는 workspace snapshot 상태입니다: {self.status}")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "progress", MappingProxyType(dict(self.progress)))
        if self.canonical_mutation:
            raise WorkspaceError("workspace preview는 Canonical State를 변경할 수 없습니다.")

    def to_record(self) -> dict[str, object]:
        return {
            "workspace_path": self.workspace_path,
            "status": self.status,
            "exists": self.exists,
            "is_directory": self.is_directory,
            "writable": self.writable,
            "plan_id": self.plan_id,
            "files": [item.to_record() for item in self.files],
            "progress": dict(self.progress),
            "canonical_mutation": False,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceChange:
    """One externally observed change between two read-only workspace scans."""

    path: str
    kind: str
    previous_sha256: str | None
    current_sha256: str | None

    def __post_init__(self) -> None:
        _normalise_relative_path(self.path)
        if self.kind not in {"ADDED", "MODIFIED", "REMOVED"}:
            raise WorkspaceError(f"지원하지 않는 workspace 변경 종류입니다: {self.kind}")

    def to_record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "previous_sha256": self.previous_sha256,
            "current_sha256": self.current_sha256,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceObservation:
    """Read-only scan plus the deterministic changes since the caller's hashes."""

    snapshot: WorkspaceSnapshot
    changes: tuple[WorkspaceChange, ...]
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", tuple(sorted(self.changes, key=lambda item: item.path)))
        if self.canonical_mutation:
            raise WorkspaceError("workspace observation은 Canonical State를 변경할 수 없습니다.")

    def to_record(self) -> dict[str, object]:
        return {
            "workspace": self.snapshot.to_record(),
            "changes": [change.to_record() for change in self.changes],
            "canonical_mutation": False,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceImpact:
    plan_id: str
    changed_path: str
    affected_paths: tuple[str, ...]
    reasons: Mapping[str, str]
    card_lenses: tuple[str, ...]
    card_ids: tuple[str, ...]
    event_kind: str = "file_modified"
    status: str = "PROPOSED"
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        _normalise_relative_path(self.changed_path)
        if self.status != "PROPOSED" or self.canonical_mutation:
            raise WorkspaceError("workspace 영향 분석은 PROPOSED 읽기 전용이어야 합니다.")
        object.__setattr__(self, "affected_paths", tuple(self.affected_paths))
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))
        object.__setattr__(self, "card_lenses", tuple(dict.fromkeys(self.card_lenses)))
        object.__setattr__(self, "card_ids", tuple(dict.fromkeys(self.card_ids)))

    def to_record(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "changed_path": self.changed_path,
            "affected_paths": list(self.affected_paths),
            "reasons": dict(self.reasons),
            "card_lenses": list(self.card_lenses),
            "card_ids": list(self.card_ids),
            "event_kind": self.event_kind,
            "status": self.status,
            "canonical_mutation": False,
        }


@dataclass(frozen=True, slots=True)
class FileProposal:
    id: str
    plan_id: str
    workspace_path: str
    path: str
    content: str
    content_sha256: str
    base_sha256: str | None
    owner: str
    source_id: str
    rationale: str
    impact: WorkspaceImpact
    create_workspace: bool = False
    status: str = "PROPOSED"
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.plan_id.strip() or not self.owner.strip() or not self.source_id.strip():
            raise WorkspaceError("FileProposal id/plan/owner/source가 필요합니다.")
        _normalise_relative_path(self.path)
        if self.impact.plan_id != self.plan_id or self.impact.changed_path != self.path:
            raise WorkspaceError("FileProposal과 영향 분석의 plan/path binding이 일치하지 않습니다.")
        if self.content_sha256 != _sha256(self.content.encode("utf-8")):
            raise WorkspaceError("FileProposal content checksum이 일치하지 않습니다.")
        if self.status not in {"PROPOSED", "APPLIED"} or self.canonical_mutation:
            raise WorkspaceError("FileProposal 상태 또는 Canonical 경계가 잘못되었습니다.")

    def to_record(self, *, include_content: bool = True) -> dict[str, object]:
        record: dict[str, object] = {
            "id": self.id,
            "plan_id": self.plan_id,
            "workspace_path": self.workspace_path,
            "path": self.path,
            "content_sha256": self.content_sha256,
            "base_sha256": self.base_sha256,
            "owner": self.owner,
            "source_id": self.source_id,
            "rationale": self.rationale,
            "impact": self.impact.to_record(),
            "create_workspace": self.create_workspace,
            "status": self.status,
            "canonical_mutation": False,
        }
        if include_content:
            record["content"] = self.content
        return record


@dataclass(frozen=True, slots=True)
class WorkspaceApplyResult:
    proposal_id: str
    workspace_path: str
    path: str
    sha256: str
    status: str = "APPLIED"
    canonical_mutation: bool = False

    def to_record(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "workspace_path": self.workspace_path,
            "path": self.path,
            "sha256": self.sha256,
            "status": self.status,
            "canonical_mutation": False,
        }


def _file_state(plan: ScaffoldPlan, root: Path, item: Any) -> WorkspaceFileState:
    path = _normalise_relative_path(item.path)
    suggested = _starter_content(plan, path, item.title, item.purpose)
    try:
        target = _safe_target(root, path)
    except WorkspaceError as exc:
        return WorkspaceFileState(
            path=path, title=item.title, purpose=item.purpose, required=item.required,
            status="UNSAFE", exists=False, size=0, sha256=None, content="",
            suggested_content=suggested, detail=str(exc),
        )
    if not target.exists():
        return WorkspaceFileState(
            path=path, title=item.title, purpose=item.purpose, required=item.required,
            status="MISSING", exists=False, size=0, sha256=None, content="",
            suggested_content=suggested, detail="아직 파일이 없습니다.",
        )
    if not target.is_file():
        return WorkspaceFileState(
            path=path, title=item.title, purpose=item.purpose, required=item.required,
            status="UNSAFE", exists=True, size=0, sha256=None, content="",
            suggested_content=suggested, detail="파일이 아닌 경로입니다.",
        )
    try:
        data = target.read_bytes()
    except OSError as exc:
        return WorkspaceFileState(
            path=path, title=item.title, purpose=item.purpose, required=item.required,
            status="UNSAFE", exists=True, size=0, sha256=None, content="",
            suggested_content=suggested, detail=f"읽을 수 없습니다: {exc}",
        )
    digest = _sha256(data)
    if len(data) > _MAX_READ_BYTES:
        return WorkspaceFileState(
            path=path, title=item.title, purpose=item.purpose, required=item.required,
            status="TOO_LARGE", exists=True, size=len(data), sha256=digest, content="",
            suggested_content=suggested, detail="파일이 너무 커서 편집기에 불러오지 않았습니다.",
        )
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return WorkspaceFileState(
            path=path, title=item.title, purpose=item.purpose, required=item.required,
            status="BINARY", exists=True, size=len(data), sha256=digest, content="",
            suggested_content=suggested, detail="UTF-8 텍스트 파일이 아닙니다.",
        )
    return WorkspaceFileState(
        path=path, title=item.title, purpose=item.purpose, required=item.required,
        status="READY" if content.strip() else "EMPTY", exists=True, size=len(data),
        sha256=digest, content=content, suggested_content=suggested,
        detail="읽기 전용 미리보기입니다.",
        substantive=has_substantive_workspace_content(content, scaffold_purpose=item.purpose),
    )


def inspect_workspace(plan: ScaffoldPlan, workspace_path: str | Path) -> WorkspaceSnapshot:
    """Inspect expected scaffold files without creating or changing anything."""

    root = _resolve_workspace(workspace_path)
    exists = root.exists()
    is_directory = root.is_dir() if exists else False
    if exists and not is_directory:
        raise WorkspaceError("workspace 경로가 폴더가 아닙니다.")
    writable = is_directory and os.access(root, os.W_OK)
    files = tuple(_file_state(plan, root, item) for item in plan.files)
    completed = sum(item.status == "READY" for item in files)
    total = len(files)
    required_files = tuple(item for item in files if item.required)
    required_substantive = sum(item.substantive for item in required_files)
    required_total = len(required_files)
    return WorkspaceSnapshot(
        workspace_path=str(root),
        status="READY" if is_directory else "MISSING",
        exists=exists,
        is_directory=is_directory,
        writable=writable,
        plan_id=plan.id,
        files=files,
        progress={
            "completed": completed,
            "total": total,
            "percent": round((completed / total) * 100) if total else 0,
            "required_substantive_completed": required_substantive,
            "required_total": required_total,
            "required_substantive_percent": (
                round((required_substantive / required_total) * 100) if required_total else 0
            ),
        },
    )


def build_file_impact(plan: ScaffoldPlan, changed_path: str) -> WorkspaceImpact:
    """Map one changed scaffold file to deterministic downstream dependents."""

    changed = _normalise_relative_path(changed_path)
    known = tuple(item.path for item in plan.files)
    if changed not in known:
        raise WorkspaceError(f"scaffold에 없는 파일입니다: {changed}")
    if changed == "synapse.project.yaml":
        affected = tuple(path for path in known if path != changed)
        reason = "프로젝트 manifest가 바뀌면 전체 구조 설명과 단계의 기준을 다시 확인합니다."
        lenses = ("structure", "causal")
        cards = ("core/decompose", "core/evidence")
    elif changed == "README.md":
        affected = ()
        reason = "README는 안내 투영이라 구조 파일을 자동으로 덮어쓰지 않습니다."
        lenses = ("structure",)
        cards = ("core/evidence",)
    else:
        prefix = changed.split("/", 1)[0]
        order = {"00_intake": 0, "01_context": 1, "02_model": 2, "03_rules": 3, "04_work": 4, "99_review": 5}
        rank = order.get(prefix, 5)
        affected = tuple(
            path for path in known
            if path != changed and order.get(path.split("/", 1)[0], 5) > rank
        )
        reason = {
            "00_intake": "아이디어가 바뀌면 목표·모델·규칙·작업·검토를 모두 재확인합니다.",
            "01_context": "범위와 제약이 바뀌면 모델·규칙·작업·검토의 정합성을 재확인합니다.",
            "02_model": "대상과 관계가 바뀌면 규칙·작업·검토의 참조를 재확인합니다.",
            "03_rules": "정합성 규칙이 바뀌면 작업 순서와 미해결 질문을 재확인합니다.",
            "04_work": "다음 작업이 바뀌면 미해결 질문과 완료 조건을 재확인합니다.",
            "99_review": "검토 메모는 자동 종속 파일을 만들지 않고 보류 상태로 남깁니다.",
        }.get(prefix, "알려진 의존성이 없어 변경 파일만 Proposal로 유지합니다.")
        lenses = ("structure", "causal") if affected else ("structure",)
        cards = ("core/decompose", "core/evidence") if affected else ("core/evidence",)
    reasons = {path: reason for path in affected}
    return WorkspaceImpact(
        plan_id=plan.id,
        changed_path=changed,
        affected_paths=affected,
        reasons=reasons,
        card_lenses=lenses,
        card_ids=cards,
    )


def observe_workspace_changes(
    plan: ScaffoldPlan,
    workspace_path: str | Path,
    previous_hashes: Mapping[str, str | None],
) -> WorkspaceObservation:
    """Scan expected files and detect external changes without reading beyond the plan."""

    snapshot = inspect_workspace(plan, workspace_path)
    current = {item.path: item.sha256 for item in snapshot.files}
    previous = {_normalise_relative_path(path): value for path, value in previous_hashes.items()}
    unknown_previous = sorted(set(previous) - set(current))
    if unknown_previous:
        raise WorkspaceError(f"scaffold에 없는 이전 파일 hash입니다: {unknown_previous}")
    changes: list[WorkspaceChange] = []
    for path in sorted(set(current) | set(previous)):
        before = previous.get(path)
        after = current.get(path)
        if before == after:
            continue
        kind = "ADDED" if before is None and after is not None else "REMOVED" if before is not None and after is None else "MODIFIED"
        changes.append(
            WorkspaceChange(
                path=path,
                kind=kind,
                previous_sha256=before,
                current_sha256=after,
            )
        )
    return WorkspaceObservation(snapshot=snapshot, changes=tuple(changes))


def propose_file_update(
    plan: ScaffoldPlan,
    workspace_path: str | Path,
    path: str,
    content: str,
    *,
    owner: str = "human",
    source_id: str = "guided-ui",
    rationale: str = "",
    create_workspace: bool = False,
) -> FileProposal:
    """Build a content Proposal and impact report without writing the file."""

    root = _resolve_workspace(workspace_path)
    relative = _normalise_relative_path(path)
    if relative not in {item.path for item in plan.files}:
        raise WorkspaceError(f"scaffold에 없는 파일입니다: {relative}")
    if not isinstance(content, str):
        raise WorkspaceError("파일 내용은 텍스트여야 합니다.")
    if len(content.encode("utf-8")) > _MAX_READ_BYTES:
        raise WorkspaceError("Proposal 파일 내용이 너무 큽니다.")
    target = _safe_target(root, relative)
    base: str | None = None
    if target.exists():
        if not target.is_file():
            raise WorkspaceError("Proposal 대상이 일반 파일이 아닙니다.")
        data = target.read_bytes()
        if len(data) > _MAX_READ_BYTES:
            raise WorkspaceError("기존 파일이 너무 커서 안전하게 Proposal할 수 없습니다.")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("기존 파일이 UTF-8 텍스트가 아닙니다.") from exc
        base = _sha256(data)
    impact = build_file_impact(plan, relative)
    owner_value = str(owner).strip() or "human"
    source_value = str(source_id).strip() or "guided-ui"
    content_hash = _sha256(content.encode("utf-8"))
    canonical = "|".join((plan.id, str(root), relative, content_hash, base or "", owner_value, source_value))
    proposal_id = f"workspace-proposal:{_sha256(canonical.encode('utf-8'))}"
    return FileProposal(
        id=proposal_id,
        plan_id=plan.id,
        workspace_path=str(root),
        path=relative,
        content=content,
        content_sha256=content_hash,
        base_sha256=base,
        owner=owner_value,
        source_id=source_value,
        rationale=str(rationale).strip(),
        impact=impact,
        create_workspace=bool(create_workspace),
    )


def apply_file_proposal(proposal: FileProposal, *, approved: bool = False) -> WorkspaceApplyResult:
    """Apply one proposal only after explicit approval and a base-hash check."""

    if proposal.status != "PROPOSED":
        raise WorkspaceError("이미 처리된 Proposal은 다시 적용할 수 없습니다.")
    if not approved:
        raise WorkspaceError("파일 저장은 명시적인 approved=true가 필요합니다.")
    root = Path(proposal.workspace_path).resolve(strict=False)
    if root.exists() and not root.is_dir():
        raise WorkspaceError("workspace 경로가 폴더가 아닙니다.")
    if not root.exists():
        if not proposal.create_workspace:
            raise WorkspaceError("workspace 폴더가 없습니다. 생성 승인을 먼저 지정하세요.")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"workspace 폴더를 만들 수 없습니다: {root}") from exc
    if not os.access(root, os.W_OK):
        raise WorkspaceError("workspace 폴더에 쓸 수 없습니다.")
    target = _safe_target(root, proposal.path)
    current = target.read_bytes() if target.exists() and target.is_file() else None
    current_hash = _sha256(current) if current is not None else None
    if current_hash != proposal.base_sha256:
        raise WorkspaceError("파일이 Proposal 이후 바뀌었습니다. 다시 읽고 영향 분석을 해 주세요.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"workspace 파일 폴더를 만들 수 없습니다: {target.parent}") from exc
    data = proposal.content.encode("utf-8")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=str(target.parent), prefix=".synapse-", suffix=".tmp") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, target)
    except OSError as exc:
        raise WorkspaceError(f"workspace 파일을 적용할 수 없습니다: {target}") from exc
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return WorkspaceApplyResult(
        proposal_id=proposal.id,
        workspace_path=str(root),
        path=proposal.path,
        sha256=proposal.content_sha256,
    )


def apply_file_proposal_set(
    proposals: tuple[FileProposal, ...],
    *,
    approved: bool = False,
) -> tuple[WorkspaceApplyResult, ...]:
    """Apply a multi-file Proposal after preflighting every base hash.

    The files are prepared and checked as one set.  If an unexpected write
    failure occurs after one file has changed, the exact previous bytes are
    restored for the files touched by this call.
    """

    if not approved:
        raise WorkspaceError("파일 묶음 저장은 명시적인 approved=true가 필요합니다.")
    if not proposals:
        raise WorkspaceError("저장할 파일 Proposal이 없습니다.")
    roots = {proposal.workspace_path for proposal in proposals}
    plan_ids = {proposal.plan_id for proposal in proposals}
    paths = [proposal.path for proposal in proposals]
    if len(roots) != 1 or len(plan_ids) != 1 or len(paths) != len(set(paths)):
        raise WorkspaceError("파일 묶음 Proposal의 workspace, plan 또는 경로가 일치하지 않습니다.")
    root = Path(next(iter(roots))).resolve(strict=False)
    snapshots: list[tuple[Path, bool, bytes | None]] = []
    for proposal in proposals:
        if proposal.status != "PROPOSED":
            raise WorkspaceError("이미 처리된 Proposal은 파일 묶음에 넣을 수 없습니다.")
        target = _safe_target(root, proposal.path)
        current = target.read_bytes() if target.exists() and target.is_file() else None
        current_hash = _sha256(current) if current is not None else None
        if current_hash != proposal.base_sha256:
            raise WorkspaceError(f"파일이 Proposal 이후 바뀌었습니다: {proposal.path}")
        snapshots.append((target, current is not None, current))
    results: list[WorkspaceApplyResult] = []
    try:
        for proposal in proposals:
            results.append(apply_file_proposal(proposal, approved=True))
    except Exception:
        for target, existed, data in reversed(snapshots[: len(results)]):
            try:
                if existed and data is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                elif target.exists():
                    target.unlink()
            except OSError:
                pass
        raise
    return tuple(results)


__all__ = [
    "FileProposal",
    "WorkspaceApplyResult",
    "WorkspaceChange",
    "WorkspaceError",
    "WorkspaceFileState",
    "WorkspaceImpact",
    "WorkspaceObservation",
    "WorkspaceSnapshot",
    "apply_file_proposal",
    "apply_file_proposal_set",
    "build_file_impact",
    "has_substantive_workspace_content",
    "inspect_workspace",
    "observe_workspace_changes",
    "propose_file_update",
]
