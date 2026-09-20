"""Deterministic bridge from a local model response to workspace Proposals.

The model is allowed to suggest text for an already planned scaffold only.  A
strict JSON parser, the existing Candidate verifier, and base-file hashes keep
the response outside Canonical State and outside the filesystem until a human
approves the resulting Proposal set.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from synapse.core.action import ActionRoute
from synapse.core.candidate import (
    CandidateMutation,
    VerificationReceipt,
    candidate_from_table,
    verify_candidate,
)
from synapse.core.contracts import LifecycleStatus
from synapse.core.scaffold import ScaffoldPlan
from synapse.core.table import CognitiveTable
from synapse.core.workspace import (
    FileProposal,
    has_substantive_workspace_content,
    inspect_workspace,
    propose_file_update,
)
from synapse.runtime.contracts import RuntimeResponse

_MAX_RESPONSE_BYTES = 512 * 1024
_RESPONSE_KEYS = {"summary", "files", "questions", "unresolved"}


class WorkspaceCandidateError(ValueError):
    """Raised when a model response cannot become a safe workspace Candidate."""


@dataclass(frozen=True, slots=True)
class StructuredWorkspaceResponse:
    """The only model output shape accepted by the workspace bridge."""

    files: Mapping[str, str]
    summary: str = ""
    questions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.files:
            raise WorkspaceCandidateError("모델 응답에 files가 하나 이상 필요합니다.")
        normalized = {str(path): str(content) for path, content in self.files.items()}
        if any(not path.strip() for path in normalized):
            raise WorkspaceCandidateError("모델 응답 files에 빈 경로가 있습니다.")
        if any(len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES for content in normalized.values()):
            raise WorkspaceCandidateError("모델 응답 파일 내용이 너무 큽니다.")
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "summary", str(self.summary).strip())
        object.__setattr__(self, "questions", _string_list(self.questions, "questions"))
        object.__setattr__(self, "unresolved", _string_list(self.unresolved, "unresolved"))

    def to_record(self) -> dict[str, object]:
        return {
            "files": dict(self.files),
            "summary": self.summary,
            "questions": list(self.questions),
            "unresolved": list(self.unresolved),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceProposalSet:
    """Several file Proposals that share one Candidate and approval boundary."""

    id: str
    candidate_id: str
    plan_id: str
    workspace_path: str
    owner: str
    source_id: str
    proposals: tuple[FileProposal, ...]
    summary: str = ""
    questions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    status: str = "PROPOSED"
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.candidate_id.strip() or not self.plan_id.strip():
            raise WorkspaceCandidateError("WorkspaceProposalSet 식별자가 필요합니다.")
        if not self.workspace_path.strip() or not self.owner.strip() or not self.source_id.strip():
            raise WorkspaceCandidateError("WorkspaceProposalSet workspace/owner/source가 필요합니다.")
        proposals = tuple(self.proposals)
        if not proposals or len({proposal.path for proposal in proposals}) != len(proposals):
            raise WorkspaceCandidateError("WorkspaceProposalSet에는 서로 다른 파일 Proposal이 필요합니다.")
        if any(proposal.plan_id != self.plan_id or proposal.workspace_path != self.workspace_path for proposal in proposals):
            raise WorkspaceCandidateError("WorkspaceProposalSet의 파일 Proposal 계약이 일치하지 않습니다.")
        if self.status not in {"PROPOSED", "APPLIED"} or self.canonical_mutation:
            raise WorkspaceCandidateError("WorkspaceProposalSet 상태 또는 Canonical 경계가 잘못되었습니다.")
        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "summary", str(self.summary).strip())
        object.__setattr__(self, "questions", _string_list(self.questions, "questions"))
        object.__setattr__(self, "unresolved", _string_list(self.unresolved, "unresolved"))

    def to_record(self, *, include_content: bool = True) -> dict[str, object]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "plan_id": self.plan_id,
            "workspace_path": self.workspace_path,
            "owner": self.owner,
            "source_id": self.source_id,
            "proposals": [proposal.to_record(include_content=include_content) for proposal in self.proposals],
            "summary": self.summary,
            "questions": list(self.questions),
            "unresolved": list(self.unresolved),
            "status": self.status,
            "canonical_mutation": False,
        }


def _string_list(value: Sequence[str] | tuple[str, ...], label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise WorkspaceCandidateError(f"{label}는 문자열 배열이어야 합니다.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise WorkspaceCandidateError(f"{label}에는 비어 있지 않은 문자열만 허용됩니다.")
        result.append(item.strip())
    return tuple(dict.fromkeys(result))


def _json_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        raise WorkspaceCandidateError("모델 응답이 비어 있습니다.")
    if len(raw.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise WorkspaceCandidateError("모델 응답이 너무 큽니다.")
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise WorkspaceCandidateError("JSON 코드 블록이 닫히지 않았습니다.")
        raw = "\n".join(lines[1:-1]).strip()
        if lines[0].strip().lower() not in {"```", "```json"}:
            raise WorkspaceCandidateError("JSON 코드 블록만 허용됩니다.")
    return raw


def parse_workspace_response(text: str, *, allowed_paths: Sequence[str]) -> StructuredWorkspaceResponse:
    """Parse strict JSON and reject prose, unknown paths, and non-text files."""

    try:
        payload = json.loads(_json_text(text))
    except json.JSONDecodeError as exc:
        raise WorkspaceCandidateError("모델 응답은 JSON 객체여야 합니다. 설명 문장은 허용하지 않습니다.") from exc
    if not isinstance(payload, Mapping):
        raise WorkspaceCandidateError("모델 응답 최상위 값은 JSON 객체여야 합니다.")
    unknown = set(payload) - _RESPONSE_KEYS
    missing = _RESPONSE_KEYS - set(payload)
    if unknown:
        raise WorkspaceCandidateError(f"workspace response에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    if missing:
        raise WorkspaceCandidateError(f"workspace response에 필수 필드가 없습니다: {sorted(missing)}")
    files = payload.get("files")
    if not isinstance(files, Mapping) or not files:
        raise WorkspaceCandidateError("모델 응답 files는 하나 이상의 경로-내용 객체여야 합니다.")
    allowed = {str(path).replace("\\", "/") for path in allowed_paths}
    normalized: dict[str, str] = {}
    for raw_path, content in files.items():
        path = str(raw_path).replace("\\", "/").strip()
        if path not in allowed:
            raise WorkspaceCandidateError(f"scaffold에 없는 파일을 제안했습니다: {path}")
        if not isinstance(content, str):
            raise WorkspaceCandidateError(f"파일 내용은 문자열이어야 합니다: {path}")
        if len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise WorkspaceCandidateError(f"파일 내용이 너무 큽니다: {path}")
        normalized[path] = content
    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        raise WorkspaceCandidateError("summary는 문자열이어야 합니다.")
    questions = payload.get("questions", [])
    unresolved = payload.get("unresolved", [])
    if not isinstance(questions, list) or not isinstance(unresolved, list):
        raise WorkspaceCandidateError("questions와 unresolved는 문자열 배열이어야 합니다.")
    return StructuredWorkspaceResponse(
        files=normalized,
        summary=summary,
        questions=tuple(questions),
        unresolved=tuple(unresolved),
    )


def build_workspace_candidate(
    plan: ScaffoldPlan,
    workspace_path: str,
    table: CognitiveTable,
    route: ActionRoute,
    runtime_response: RuntimeResponse,
    *,
    owner: str,
    source_id: str,
    executor_id: str,
    base_hashes: Mapping[str, str | None] | None = None,
) -> tuple[CandidateMutation, StructuredWorkspaceResponse]:
    """Turn a provider response into a PROPOSED Candidate and capture bases."""

    response = parse_workspace_response(runtime_response.text, allowed_paths=[item.path for item in plan.files])
    snapshot = inspect_workspace(plan, workspace_path)
    current_hashes = {item.path: item.sha256 for item in snapshot.files}
    bases = current_hashes if base_hashes is None else base_hashes
    proposal_base_hashes = {path: bases.get(path) for path in response.files}
    changes = {
        "files": dict(response.files),
        "summary": response.summary,
        "questions": list(response.questions),
        "unresolved": list(response.unresolved),
        "base_hashes": proposal_base_hashes,
        "workspace_path": snapshot.workspace_path,
        "plan_id": plan.id,
        "source_id": str(source_id).strip() or "guided-ui",
        "provider": runtime_response.provider,
        "model": runtime_response.model,
        "executor_id": str(executor_id).strip() or runtime_response.provider,
    }
    evidence_ids = tuple(
        dict.fromkeys(
            (
                str(source_id).strip() or "guided-ui",
                runtime_response.request_id,
                table.event_id,
            )
        )
    )
    candidate = candidate_from_table(
        table,
        route,
        owner=str(owner).strip() or "human-ui",
        changes=changes,
        evidence_ids=evidence_ids,
    )
    return candidate, response


def verify_workspace_candidate(
    candidate: CandidateMutation,
    plan: ScaffoldPlan,
    workspace_path: str,
    *,
    table: CognitiveTable | None = None,
    route: ActionRoute | None = None,
) -> VerificationReceipt:
    """Verify Candidate contracts and ensure no file changed since inference."""

    receipt = verify_candidate(candidate, table=table, route=route)
    checks = dict(receipt.checks)
    errors = list(receipt.errors)
    changes = candidate.changes
    files = changes.get("files") if isinstance(changes, Mapping) else None
    bases = changes.get("base_hashes") if isinstance(changes, Mapping) else None
    allowed = {item.path for item in plan.files}
    checks["files_mapping"] = isinstance(files, Mapping) and bool(files)
    checks["paths_allowlisted"] = isinstance(files, Mapping) and set(files).issubset(allowed)
    checks["base_hashes_present"] = isinstance(bases, Mapping) and set(bases or {}) == set(files or {})
    checks["plan_matches"] = changes.get("plan_id") == plan.id if isinstance(changes, Mapping) else False
    scaffold_purposes = {item.path: item.purpose for item in plan.files}
    checks["content_substantive"] = (
        isinstance(files, Mapping)
        and bool(files)
        and all(
            path in scaffold_purposes
            and has_substantive_workspace_content(content, scaffold_purpose=scaffold_purposes[path])
            for path, content in files.items()
        )
    )
    if not checks["files_mapping"]:
        errors.append("workspace_files_missing")
    if not checks["paths_allowlisted"]:
        errors.append("workspace_path_not_allowlisted")
    if not checks["base_hashes_present"]:
        errors.append("workspace_base_hash_missing")
    if not checks["plan_matches"]:
        errors.append("workspace_plan_mismatch")
    if not checks["content_substantive"]:
        errors.append("workspace_content_empty")
    stable = True
    if checks["files_mapping"] and checks["base_hashes_present"] and checks["paths_allowlisted"]:
        snapshot = inspect_workspace(plan, workspace_path)
        current = {item.path: item.sha256 for item in snapshot.files}
        for path, expected in bases.items():
            if current.get(path) != expected:
                stable = False
                errors.append(f"workspace_changed:{path}")
    checks["workspace_bases_stable"] = stable
    canonical = json.dumps(
        {"candidate_id": candidate.id, "checks": checks, "errors": sorted(set(errors))},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return VerificationReceipt(
        id=f"verify:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        candidate_id=candidate.id,
        passed=not errors,
        checks=checks,
        errors=tuple(errors),
    )


def workspace_proposal_set_from_candidate(
    candidate: CandidateMutation,
    receipt: VerificationReceipt,
    plan: ScaffoldPlan,
    workspace_path: str,
    *,
    owner: str,
    source_id: str,
    create_workspace: bool = False,
) -> WorkspaceProposalSet:
    """Convert a passing Candidate into independent File Proposals."""

    if receipt.candidate_id != candidate.id or not receipt.passed:
        raise WorkspaceCandidateError("검증을 통과한 Candidate만 workspace Proposal로 변환할 수 있습니다.")
    if candidate.status is not LifecycleStatus.PROPOSED:
        raise WorkspaceCandidateError("PROPOSED Candidate만 workspace Proposal로 변환할 수 있습니다.")
    changes = candidate.changes
    files = changes.get("files") if isinstance(changes, Mapping) else None
    summary = str(changes.get("summary", "")) if isinstance(changes, Mapping) else ""
    if not isinstance(files, Mapping) or not files:
        raise WorkspaceCandidateError("Candidate에 workspace files가 없습니다.")
    proposals = tuple(
        propose_file_update(
            plan,
            workspace_path,
            str(path),
            content,
            owner=owner,
            source_id=source_id,
            rationale=summary,
            create_workspace=create_workspace,
        )
        for path, content in sorted(files.items())
    )
    canonical = json.dumps(
        {"candidate_id": candidate.id, "proposals": [proposal.id for proposal in proposals]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return WorkspaceProposalSet(
        id=f"workspace-proposals:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        candidate_id=candidate.id,
        plan_id=plan.id,
        workspace_path=str(Path(workspace_path).expanduser().resolve(strict=False)),
        owner=str(owner).strip() or "human-ui",
        source_id=str(source_id).strip() or "guided-ui",
        proposals=proposals,
        summary=summary,
        questions=tuple(changes.get("questions", ())),
        unresolved=tuple(changes.get("unresolved", ())),
    )


__all__ = [
    "StructuredWorkspaceResponse",
    "WorkspaceCandidateError",
    "WorkspaceProposalSet",
    "build_workspace_candidate",
    "parse_workspace_response",
    "verify_workspace_candidate",
    "workspace_proposal_set_from_candidate",
]
