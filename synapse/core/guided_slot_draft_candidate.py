"""Proposal-only content Candidate for ONE guided detail slot.

This is the parallel, skeleton-first path (DEC-0127): instead of asking a local
model for the whole structure JSON (ids, edges, statuses, roles), Synapse builds
the skeleton from presets and the model drafts only the bounded *content* of a
single ``answer_mode == "llm_draft"`` slot -- its ``summary``, ``content``,
``questions`` and ``unresolved``. Everything structural stays system-owned: the
slot id, artifact_role, file path, status, provenance, and hash.

The result is an in-memory PROPOSED Candidate. It never writes a file, creates a
workspace Proposal, executes anything, or mutates Canonical State. A model that
breaks JSON or omits a field cannot be silently repaired and cannot be promoted:
the builder raises, and the caller leaves that one slot MISSING (the failure unit
is the slot, not the whole structure).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.guided_detail import DetailSlot, GuidedDetailPlan
from synapse.core.idea_session import IdeaSeed, canonical_hash, sha256_text
from synapse.core.structure_map import ARTIFACT_ROLE_PATHS
from synapse.runtime.contracts import RuntimeResponse

SLOT_DRAFT_SCHEMA = "guided.slot-draft.v1"
_ALLOWED_PROVIDERS = {"lmstudio", "ollama"}
_DRAFT_ANSWER_MODE = "llm_draft"
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_CONTENT_BYTES = 128 * 1024

#: The model may fill exactly these keys and must echo the four identity keys.
#: It is a small, flat object on purpose -- the giant analyze schema is what this
#: path exists to avoid.
_ROOT_KEYS = {
    "schema_version",
    "seed_hash",
    "detail_plan_id",
    "slot_id",
    "artifact_role",
    "summary",
    "content",
    "questions",
    "unresolved",
}


class GuidedSlotDraftError(ValueError):
    """Raised when a slot-draft response cannot become a safe Candidate.

    A raise means this one slot's draft failed; the caller keeps the slot
    MISSING and may re-ask or fall back to user_text. It never means the whole
    plan failed, and it never results in an arbitrary fill or a CONFIRMED
    promotion.
    """


def _text(value: Any, label: str, *, required: bool = True, limit: int = 8_000) -> str:
    if not isinstance(value, str):
        raise GuidedSlotDraftError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise GuidedSlotDraftError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise GuidedSlotDraftError(f"{label}이(가) 너무 깁니다.")
    return result


def _strings(value: Any, label: str, *, limit: int = 1_000) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GuidedSlotDraftError(f"{label}는 문자열 배열이어야 합니다.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise GuidedSlotDraftError(f"{label}의 항목은 문자열이어야 합니다.")
        cleaned = item.strip()
        if not cleaned:
            continue
        if len(cleaned) > limit:
            raise GuidedSlotDraftError(f"{label}의 항목이 너무 깁니다.")
        result.append(cleaned)
    return tuple(dict.fromkeys(result))


def _json_payload(text: str) -> Mapping[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise GuidedSlotDraftError("모델 응답이 비어 있습니다.")
    if len(raw.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise GuidedSlotDraftError("모델 응답이 너무 큽니다.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuidedSlotDraftError("slot draft 응답은 JSON 객체만 허용됩니다.") from exc
    if not isinstance(payload, Mapping):
        raise GuidedSlotDraftError("slot draft 응답 최상위 값은 JSON 객체여야 합니다.")
    unknown = set(payload) - _ROOT_KEYS
    missing = _ROOT_KEYS - set(payload)
    if unknown:
        raise GuidedSlotDraftError(f"slot draft 응답에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    if missing:
        raise GuidedSlotDraftError(f"slot draft 응답에 필수 필드가 없습니다: {sorted(missing)}")
    return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedSlotDraftCandidate:
    """A bounded model draft for one slot, normalized into a PROPOSED Candidate."""

    id: str
    seed_hash: str
    detail_plan_id: str
    slot_id: str
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
            ("candidate.slot_id", self.slot_id),
            ("candidate.request_id", self.request_id),
            ("candidate.prompt_version", self.prompt_version),
            ("candidate.response_hash", self.response_hash),
            ("candidate.artifact_role", self.artifact_role),
            ("candidate.path", self.path),
            ("candidate.summary", self.summary),
            ("candidate.content", self.content),
        ):
            _text(value, label)
        provider = self.provider.strip().lower()
        if provider not in _ALLOWED_PROVIDERS:
            raise GuidedSlotDraftError("slot draft Candidate는 LM Studio 또는 Ollama만 허용합니다.")
        object.__setattr__(self, "provider", provider)
        _text(self.model, "candidate.model", limit=240)
        # The path is derived from the role by the SYSTEM, never taken from the model.
        if self.artifact_role not in ARTIFACT_ROLE_PATHS:
            raise GuidedSlotDraftError("slot draft Candidate에 알 수 없는 artifact role이 있습니다.")
        if self.path != ARTIFACT_ROLE_PATHS[self.artifact_role]:
            raise GuidedSlotDraftError("slot draft Candidate path는 artifact role에서 결정되어야 합니다.")
        if len(self.content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise GuidedSlotDraftError("slot draft Candidate content가 너무 큽니다.")
        if self.status != "PROPOSED":
            raise GuidedSlotDraftError("slot draft Candidate는 PROPOSED로 시작해야 합니다.")
        if self.canonical_mutation or self.filesystem_mutation or self.execution_allowed:
            raise GuidedSlotDraftError("slot draft Candidate는 파일/실행/Canonical 권한을 가질 수 없습니다.")
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
            "slot_id": self.slot_id,
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
            "owner": self.owner,
            "source": self.source,
            "status": self.status,
            "provenance": dict(self.provenance),
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "execution_allowed": False,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedSlotDraftVerification:
    """Deterministic verification receipt for a GuidedSlotDraftCandidate."""

    id: str
    candidate_id: str
    passed: bool
    checks: Mapping[str, bool]
    errors: tuple[str, ...] = ()
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.candidate_id.strip():
            raise GuidedSlotDraftError("slot draft verification 식별자가 필요합니다.")
        if self.canonical_mutation:
            raise GuidedSlotDraftError("slot draft verification은 Canonical을 변경할 수 없습니다.")
        normalized = {str(key): bool(value) for key, value in self.checks.items()}
        errors = tuple(dict.fromkeys(str(error).strip() for error in self.errors if str(error).strip()))
        object.__setattr__(self, "checks", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "errors", errors)
        if self.passed != (not errors and all(normalized.values())):
            raise GuidedSlotDraftError("slot draft verification passed 값이 check/error와 일치하지 않습니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "passed": self.passed,
            "checks": dict(self.checks),
            "errors": list(self.errors),
            "canonical_mutation": False,
        }


def build_slot_draft_json_schema(
    *, seed_hash: str, detail_plan_id: str, slot_id: str, artifact_role: str
) -> dict[str, Any]:
    """The one place the slot-draft response_format schema is built.

    Derived from ``_ROOT_KEYS`` -- the same set ``_json_payload`` validates
    against -- so the response_format schema and the parser's expectations
    cannot drift apart into two authorities. The four identity keys are
    ``const``-locked to the values the system already knows, so a provider
    that enforces the schema (e.g. LM Studio's json_schema mode) cannot
    generate a mismatched identity at all; the re-check in
    ``build_guided_slot_draft_candidate`` stays as defense in depth for
    providers or responses that don't honor the schema.
    """

    return {
        "type": "object",
        "properties": {
            "schema_version": {"const": SLOT_DRAFT_SCHEMA},
            "seed_hash": {"const": seed_hash},
            "detail_plan_id": {"const": detail_plan_id},
            "slot_id": {"const": slot_id},
            "artifact_role": {"const": artifact_role},
            "summary": {"type": "string"},
            "content": {"type": "string"},
            "questions": {"type": "array", "items": {"type": "string"}},
            "unresolved": {"type": "array", "items": {"type": "string"}},
        },
        "required": sorted(_ROOT_KEYS),
        "additionalProperties": False,
    }


def _find_slot(plan: GuidedDetailPlan, slot_id: str) -> DetailSlot:
    for slot in plan.slots:
        if slot.id == slot_id:
            return slot
    raise GuidedSlotDraftError(f"detail plan에 slot이 없습니다: {slot_id}")


def build_guided_slot_draft_candidate(
    seed: IdeaSeed,
    plan: GuidedDetailPlan,
    slot_id: str,
    runtime_response: RuntimeResponse,
    *,
    prompt_version: str = SLOT_DRAFT_SCHEMA,
    owner: str = "human-ui",
    source: str = "guided-ui",
) -> GuidedSlotDraftCandidate:
    """Parse one strict slot-draft response and bind it to the requested slot.

    The model may only draft a slot whose ``answer_mode`` is ``llm_draft``. It
    must echo seed_hash, detail_plan_id, slot_id and artifact_role exactly; the
    system owns those, so a mismatch is a hard error rather than something to
    reconcile.
    """

    if not isinstance(seed, IdeaSeed) or not isinstance(plan, GuidedDetailPlan):
        raise GuidedSlotDraftError("slot draft에는 IdeaSeed와 GuidedDetailPlan이 필요합니다.")
    if plan.seed_hash != seed.raw_hash:
        raise GuidedSlotDraftError("detail plan의 seed hash가 IdeaSeed와 다릅니다.")
    slot = _find_slot(plan, slot_id)
    if slot.answer_mode != _DRAFT_ANSWER_MODE:
        raise GuidedSlotDraftError(
            f"slot {slot_id}의 answer_mode는 {slot.answer_mode}입니다. "
            "모델 초안은 llm_draft slot에서만 허용됩니다."
        )
    provider = runtime_response.provider.strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise GuidedSlotDraftError("slot draft는 LM Studio 또는 Ollama만 지원합니다.")

    payload = _json_payload(runtime_response.text)
    if payload.get("schema_version") != SLOT_DRAFT_SCHEMA:
        raise GuidedSlotDraftError("지원하지 않는 slot draft schema_version입니다.")
    expected = {
        "seed_hash": seed.raw_hash,
        "detail_plan_id": plan.id,
        "slot_id": slot.id,
        "artifact_role": slot.artifact_role,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise GuidedSlotDraftError(f"{key}가 요청한 slot과 일치하지 않습니다.")

    summary = _text(payload.get("summary"), "summary", limit=4_000)
    content = _text(payload.get("content"), "content", limit=200_000)
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise GuidedSlotDraftError("content가 너무 큽니다.")
    questions = _strings(payload.get("questions"), "questions")
    unresolved = _strings(payload.get("unresolved"), "unresolved")

    response_hash = sha256_text(runtime_response.text.strip())
    provenance = {
        "provider": provider,
        "model": runtime_response.model,
        "request_id": runtime_response.request_id,
        "prompt_version": _text(prompt_version, "prompt_version", limit=120),
        "response_hash": response_hash,
        "response_finish_reason": runtime_response.finish_reason,
        "slot_id": slot.id,
    }
    identity = {
        "seed_hash": seed.raw_hash,
        "detail_plan_id": plan.id,
        "slot_id": slot.id,
        "provider": provider,
        "model": runtime_response.model,
        "request_id": runtime_response.request_id,
        "response_hash": response_hash,
        "artifact_role": slot.artifact_role,
        "content": content,
    }
    return GuidedSlotDraftCandidate(
        id=f"guided-slot-draft:{canonical_hash(identity).removeprefix('sha256:')}",
        seed_hash=seed.raw_hash,
        detail_plan_id=plan.id,
        slot_id=slot.id,
        provider=provider,
        model=runtime_response.model,
        request_id=runtime_response.request_id,
        prompt_version=prompt_version,
        response_hash=response_hash,
        artifact_role=slot.artifact_role,
        path=ARTIFACT_ROLE_PATHS[slot.artifact_role],
        summary=summary,
        content=content,
        questions=questions,
        unresolved=unresolved,
        owner=owner,
        source=source,
        provenance=provenance,
    )


def verify_guided_slot_draft_candidate(
    candidate: GuidedSlotDraftCandidate,
    seed: IdeaSeed,
    plan: GuidedDetailPlan,
) -> GuidedSlotDraftVerification:
    """Verify one slot draft against its seed and plan without any side effect."""

    slot = None
    for item in plan.slots:
        if item.id == candidate.slot_id:
            slot = item
            break

    checks = {
        "status_proposed": candidate.status == "PROPOSED",
        "provider_allowlisted": candidate.provider in _ALLOWED_PROVIDERS,
        "request_id_present": bool(candidate.request_id.strip()),
        "model_present": bool(candidate.model.strip()),
        "seed_hash_matches": candidate.seed_hash == seed.raw_hash,
        "plan_id_matches": candidate.detail_plan_id == plan.id,
        "plan_seed_matches": plan.seed_hash == seed.raw_hash,
        "slot_present": slot is not None,
        "slot_is_llm_draft": slot is not None and slot.answer_mode == _DRAFT_ANSWER_MODE,
        "role_matches_slot": slot is not None and candidate.artifact_role == slot.artifact_role,
        "role_allowlisted": candidate.artifact_role in ARTIFACT_ROLE_PATHS,
        "path_from_role": candidate.path == ARTIFACT_ROLE_PATHS.get(candidate.artifact_role, ""),
        "no_filesystem": candidate.filesystem_mutation is False,
        "no_execution": candidate.execution_allowed is False,
        "canonical_mutation_false": candidate.canonical_mutation is False,
        "response_hash_format": candidate.response_hash.startswith("sha256:"),
        "provenance_present": all(
            bool(str(candidate.provenance.get(key, "")).strip())
            for key in ("provider", "model", "request_id", "prompt_version", "response_hash")
        ),
    }
    errors = tuple(key for key, passed in checks.items() if not passed)
    receipt_seed = {"candidate_id": candidate.id, "checks": checks, "errors": errors}
    receipt_id = f"guided-slot-draft-verification:{canonical_hash(receipt_seed).removeprefix('sha256:')}"
    return GuidedSlotDraftVerification(
        id=receipt_id,
        candidate_id=candidate.id,
        passed=not errors and all(checks.values()),
        checks=checks,
        errors=errors,
    )


__all__ = [
    "SLOT_DRAFT_SCHEMA",
    "GuidedSlotDraftCandidate",
    "GuidedSlotDraftError",
    "GuidedSlotDraftVerification",
    "build_guided_slot_draft_candidate",
    "build_slot_draft_json_schema",
    "verify_guided_slot_draft_candidate",
]
