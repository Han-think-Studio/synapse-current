"""Proposal-only content Candidate for one reviewed Guided WorkUnit.

The first WorkUnit preview is deterministic.  This module adds the next,
explicitly dispatched step: a local model may draft content for the preview's
single logical artifact role.  The result remains an in-memory PROPOSED
Candidate; it never writes a file, creates a workspace Proposal, executes a
WorkUnit, or mutates Canonical State.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.guided_work_unit import GuidedWorkUnitPreview
from synapse.core.idea_session import IdeaSeed, canonical_hash, canonical_json, sha256_text
from synapse.core.structure_map import ARTIFACT_ROLE_PATHS
from synapse.runtime.contracts import RuntimeResponse

_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_CONTENT_BYTES = 256 * 1024
_ALLOWED_PROVIDERS = {"lmstudio", "ollama"}
_ROOT_KEYS = {
    "schema_version",
    "seed_hash",
    "detail_plan_id",
    "work_unit_preview_id",
    "artifact_role",
    "summary",
    "content",
    "questions",
    "unresolved",
    "completion_check",
}
WORK_UNIT_CANDIDATE_RESPONSE_KEYS = _ROOT_KEYS


class GuidedWorkUnitCandidateError(ValueError):
    """Raised when a WorkUnit draft cannot become a safe Candidate."""


def _text(value: Any, label: str, *, required: bool = True, limit: int = 8_000) -> str:
    if not isinstance(value, str):
        raise GuidedWorkUnitCandidateError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise GuidedWorkUnitCandidateError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise GuidedWorkUnitCandidateError(f"{label}이(가) 너무 깁니다.")
    return result


def _strings(value: Any, label: str, *, limit: int = 1_000) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GuidedWorkUnitCandidateError(f"{label}는 문자열 배열이어야 합니다.")
    result: list[str] = []
    for item in value:
        result.append(_text(item, label, limit=limit))
    return tuple(dict.fromkeys(result))


def _json_payload(text: str) -> Mapping[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise GuidedWorkUnitCandidateError("모델 응답이 비어 있습니다.")
    if len(raw.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise GuidedWorkUnitCandidateError("모델 응답이 너무 큽니다.")
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"}:
            raise GuidedWorkUnitCandidateError("모델 응답 JSON 코드블록 형식이 올바르지 않습니다.")
        if lines[-1].strip() != "```":
            raise GuidedWorkUnitCandidateError("모델 응답 JSON 코드블록이 닫히지 않았습니다.")
        raw = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        decoder = json.JSONDecoder()
        candidates: list[Mapping[str, Any]] = []
        for index, character in enumerate(raw):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(raw[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, Mapping) and set(candidate) == _ROOT_KEYS:
                candidates.append(candidate)
        if len(candidates) != 1:
            raise GuidedWorkUnitCandidateError(
                "모델 응답에서 유일한 guided WorkUnit JSON 객체를 찾을 수 없습니다."
            ) from exc
        payload = candidates[0]
    if not isinstance(payload, Mapping):
        raise GuidedWorkUnitCandidateError("모델 응답 최상위 값은 JSON 객체여야 합니다.")
    unknown = set(payload) - _ROOT_KEYS
    missing = _ROOT_KEYS - set(payload)
    if unknown:
        raise GuidedWorkUnitCandidateError(f"work-unit response에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    if missing:
        raise GuidedWorkUnitCandidateError(f"work-unit response에 필수 필드가 없습니다: {sorted(missing)}")
    return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedWorkUnitCandidate:
    """One verified-but-unapplied draft for a deterministic WorkUnit preview."""

    id: str
    seed_hash: str
    detail_plan_id: str
    work_unit_preview_id: str
    provider: str
    model: str
    request_id: str
    prompt_version: str
    response_hash: str
    artifact_role: str
    path: str
    summary: str
    content: str
    questions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    completion_check: str = ""
    owner: str = "human-ui"
    source: str = "guided-ui"
    status: str = "PROPOSED"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    canonical_mutation: bool = False
    filesystem_mutation: bool = False
    execution_allowed: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("candidate.id", self.id),
            ("candidate.seed_hash", self.seed_hash),
            ("candidate.detail_plan_id", self.detail_plan_id),
            ("candidate.work_unit_preview_id", self.work_unit_preview_id),
            ("candidate.request_id", self.request_id),
            ("candidate.prompt_version", self.prompt_version),
            ("candidate.response_hash", self.response_hash),
            ("candidate.artifact_role", self.artifact_role),
            ("candidate.path", self.path),
            ("candidate.summary", self.summary),
            ("candidate.content", self.content),
            ("candidate.completion_check", self.completion_check),
        ):
            _text(value, label)
        provider = self.provider.strip().lower()
        if provider not in _ALLOWED_PROVIDERS:
            raise GuidedWorkUnitCandidateError("WorkUnit Candidate는 LM Studio 또는 Ollama만 허용합니다.")
        object.__setattr__(self, "provider", provider)
        _text(self.model, "candidate.model", limit=240)
        if self.artifact_role not in ARTIFACT_ROLE_PATHS:
            raise GuidedWorkUnitCandidateError("WorkUnit Candidate에 알 수 없는 artifact role이 있습니다.")
        if self.path != ARTIFACT_ROLE_PATHS[self.artifact_role]:
            raise GuidedWorkUnitCandidateError("WorkUnit Candidate path는 artifact role에서 결정되어야 합니다.")
        if len(self.content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise GuidedWorkUnitCandidateError("WorkUnit Candidate content가 너무 큽니다.")
        if self.status != "PROPOSED":
            raise GuidedWorkUnitCandidateError("WorkUnit Candidate는 PROPOSED로 시작해야 합니다.")
        if self.canonical_mutation or self.filesystem_mutation or self.execution_allowed:
            raise GuidedWorkUnitCandidateError("WorkUnit Candidate는 파일/실행/Canonical 권한을 가질 수 없습니다.")
        object.__setattr__(self, "questions", _strings(list(self.questions), "candidate.questions"))
        object.__setattr__(self, "unresolved", _strings(list(self.unresolved), "candidate.unresolved"))
        object.__setattr__(self, "owner", _text(self.owner, "candidate.owner", limit=160))
        object.__setattr__(self, "source", _text(self.source, "candidate.source", limit=160))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seed_hash": self.seed_hash,
            "detail_plan_id": self.detail_plan_id,
            "work_unit_preview_id": self.work_unit_preview_id,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "prompt_version": self.prompt_version,
            "response_hash": self.response_hash,
            "artifact_role": self.artifact_role,
            "path": self.path,
            "summary": self.summary,
            "content": self.content,
            "questions": list(self.questions),
            "unresolved": list(self.unresolved),
            "completion_check": self.completion_check,
            "owner": self.owner,
            "source": self.source,
            "status": self.status,
            "provenance": dict(self.provenance),
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "execution_allowed": False,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedWorkUnitVerification:
    """Deterministic receipt for a WorkUnit Candidate."""

    id: str
    candidate_id: str
    passed: bool
    checks: Mapping[str, bool]
    errors: tuple[str, ...] = ()
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.candidate_id.strip():
            raise GuidedWorkUnitCandidateError("WorkUnit verification 식별자가 필요합니다.")
        if self.canonical_mutation:
            raise GuidedWorkUnitCandidateError("WorkUnit verification은 Canonical을 변경할 수 없습니다.")
        normalized = {str(key): bool(value) for key, value in self.checks.items()}
        errors = tuple(dict.fromkeys(str(error).strip() for error in self.errors if str(error).strip()))
        object.__setattr__(self, "checks", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "errors", errors)
        if self.passed != (not errors and all(normalized.values())):
            raise GuidedWorkUnitCandidateError("WorkUnit verification passed 값이 check/error와 일치하지 않습니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "passed": self.passed,
            "checks": dict(self.checks),
            "errors": list(self.errors),
            "canonical_mutation": False,
        }


def build_work_unit_candidate_json_schema(
    seed_hash: str,
    detail_plan_id: str,
    work_unit_preview_id: str,
    artifact_roles: tuple[str, ...],
) -> dict[str, Any]:
    """Build a response_format JSON Schema for the work-unit-candidate call.

    Mirrors Phase 68/69's design: the four identity fields the parser above
    already checks are const-locked instead of merely instructed, and
    artifact_role is scoped to exactly this preview's own roles (tighter
    than the full ARTIFACT_ROLE_PATHS enum, since only those are valid here).
    Deliberately no minLength/uniqueItems -- verified elsewhere (Phase 69) to
    cost ~5x grammar-compile time for no shape benefit; type/required/enum
    already catch non-JSON, missing-field, wrong-type, and bad-enum
    failures, which is what a schema can usefully enforce.
    """

    string_array = {"type": "array", "items": {"type": "string"}}
    properties = {
        "schema_version": {"const": "guided.work-unit.v1"},
        "seed_hash": {"const": seed_hash},
        "detail_plan_id": {"const": detail_plan_id},
        "work_unit_preview_id": {"const": work_unit_preview_id},
        "artifact_role": {"type": "string", "enum": sorted(artifact_roles)},
        "summary": {"type": "string"},
        "content": {"type": "string"},
        "questions": string_array,
        "unresolved": string_array,
        "completion_check": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(WORK_UNIT_CANDIDATE_RESPONSE_KEYS),
        "additionalProperties": False,
    }


def build_guided_work_unit_candidate(
    seed: IdeaSeed,
    preview: GuidedWorkUnitPreview,
    runtime_response: RuntimeResponse,
    *,
    prompt_version: str = "guided.work-unit.v1",
    owner: str = "human-ui",
    source: str = "guided-ui",
) -> GuidedWorkUnitCandidate:
    """Parse one strict response and bind it to the revalidated READY preview."""

    if not isinstance(seed, IdeaSeed) or not isinstance(preview, GuidedWorkUnitPreview):
        raise GuidedWorkUnitCandidateError("WorkUnit Candidate에는 IdeaSeed와 WorkUnit preview가 필요합니다.")
    if preview.status != "READY":
        raise GuidedWorkUnitCandidateError("BLOCKED WorkUnit preview에서는 Candidate를 만들 수 없습니다.")
    if runtime_response.provider.strip().lower() not in _ALLOWED_PROVIDERS:
        raise GuidedWorkUnitCandidateError("WorkUnit Candidate는 LM Studio 또는 Ollama만 허용합니다.")
    payload = _json_payload(runtime_response.text)
    if payload.get("schema_version") != "guided.work-unit.v1":
        raise GuidedWorkUnitCandidateError("지원하지 않는 work-unit schema_version입니다.")
    expected = {
        "seed_hash": seed.raw_hash,
        "detail_plan_id": preview.detail_plan_id,
        "work_unit_preview_id": preview.id,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise GuidedWorkUnitCandidateError(f"{key}가 현재 WorkUnit preview와 일치하지 않습니다.")
    role = _text(payload.get("artifact_role"), "artifact_role", limit=80)
    if role not in preview.artifact_roles:
        raise GuidedWorkUnitCandidateError("WorkUnit Candidate artifact_role이 preview와 일치하지 않습니다.")
    content = _text(payload.get("content"), "content", limit=250_000)
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise GuidedWorkUnitCandidateError("content가 너무 큽니다.")
    summary = _text(payload.get("summary"), "summary", limit=4_000)
    completion = _text(payload.get("completion_check"), "completion_check", limit=4_000)
    questions = _strings(payload.get("questions"), "questions")
    unresolved = _strings(payload.get("unresolved"), "unresolved")
    response_hash = sha256_text(runtime_response.text.strip())
    provider = runtime_response.provider.strip().lower()
    provenance = {
        "provider": provider,
        "model": runtime_response.model,
        "request_id": runtime_response.request_id,
        "prompt_version": _text(prompt_version, "prompt_version", limit=120),
        "response_hash": response_hash,
        "response_finish_reason": runtime_response.finish_reason,
    }
    identity = {
        "seed_hash": seed.raw_hash,
        "detail_plan_id": preview.detail_plan_id,
        "preview_id": preview.id,
        "provider": provider,
        "model": runtime_response.model,
        "request_id": runtime_response.request_id,
        "response_hash": response_hash,
        "artifact_role": role,
        "content": content,
    }
    return GuidedWorkUnitCandidate(
        id=f"guided-work-unit-candidate:{canonical_hash(identity).removeprefix('sha256:')}",
        seed_hash=seed.raw_hash,
        detail_plan_id=preview.detail_plan_id,
        work_unit_preview_id=preview.id,
        provider=provider,
        model=runtime_response.model,
        request_id=runtime_response.request_id,
        prompt_version=prompt_version,
        response_hash=response_hash,
        artifact_role=role,
        path=ARTIFACT_ROLE_PATHS[role],
        summary=summary,
        content=content,
        questions=questions,
        unresolved=unresolved,
        completion_check=completion,
        owner=owner,
        source=source,
        provenance=provenance,
    )


def verify_guided_work_unit_candidate(
    candidate: GuidedWorkUnitCandidate,
    seed: IdeaSeed,
    preview: GuidedWorkUnitPreview,
) -> GuidedWorkUnitVerification:
    """Verify the binding and the no-mutation boundary without I/O."""

    checks = {
        "status_proposed": candidate.status == "PROPOSED",
        "provider_allowlisted": candidate.provider in _ALLOWED_PROVIDERS,
        "request_id_present": bool(candidate.request_id.strip()),
        "model_present": bool(candidate.model.strip()),
        "seed_matches": candidate.seed_hash == seed.raw_hash == preview.seed_hash,
        "detail_plan_matches": candidate.detail_plan_id == preview.detail_plan_id,
        "preview_matches": candidate.work_unit_preview_id == preview.id,
        "preview_ready": preview.status == "READY",
        "artifact_role_allowlisted": candidate.artifact_role in preview.artifact_roles,
        "path_derived": candidate.path == ARTIFACT_ROLE_PATHS.get(candidate.artifact_role),
        "content_present": bool(candidate.content.strip()),
        "summary_present": bool(candidate.summary.strip()),
        "completion_check_present": bool(candidate.completion_check.strip()),
        "response_hash_format": candidate.response_hash.startswith("sha256:"),
        "provenance_matches": (
            candidate.provenance.get("provider") == candidate.provider
            and candidate.provenance.get("request_id") == candidate.request_id
            and candidate.provenance.get("response_hash") == candidate.response_hash
        ),
        "canonical_mutation_false": candidate.canonical_mutation is False,
        "filesystem_mutation_false": candidate.filesystem_mutation is False,
        "execution_disallowed": candidate.execution_allowed is False,
    }
    errors = tuple(key for key, passed in checks.items() if not passed)
    receipt_seed = {"candidate_id": candidate.id, "checks": checks, "errors": errors}
    return GuidedWorkUnitVerification(
        id=f"guided-work-unit-verification:{hashlib.sha256(canonical_json(receipt_seed).encode('utf-8')).hexdigest()}",
        candidate_id=candidate.id,
        passed=not errors and all(checks.values()),
        checks=checks,
        errors=errors,
    )


__all__ = [
    "WORK_UNIT_CANDIDATE_RESPONSE_KEYS",
    "GuidedWorkUnitCandidate",
    "GuidedWorkUnitCandidateError",
    "GuidedWorkUnitVerification",
    "build_guided_work_unit_candidate",
    "build_work_unit_candidate_json_schema",
    "verify_guided_work_unit_candidate",
]
