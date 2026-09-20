"""Phase 28B bridge from an explicit local-model response to a safe Candidate.

This module accepts one strict combined analysis response and delegates the
actual expansion/structure field validation to the Phase 28A parsers.  The
result is an in-memory PROPOSED Candidate only; it never writes files, calls a
provider, or mutates Canonical State.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.idea_expansion import (
    _EXPANSION_KEYS,
    _QUESTION_KEYS,
    IdeaExpansionError,
    IdeaExpansionProposal,
    parse_idea_expansion_response,
)
from synapse.core.idea_session import IdeaSeed, canonical_hash, canonical_json, sha256_text
from synapse.core.structure_map import (
    ARTIFACT_ROLE_PATHS,
    EDGE_RELATIONS,
    EDGE_RESPONSE_KEYS,
    NODE_KINDS,
    NODE_RESPONSE_KEYS,
    NODE_STATUSES,
    WORK_STATUSES,
    WORK_UNIT_RESPONSE_KEYS,
    StructureMapError,
    StructureMapProposal,
    parse_structure_map_response,
)
from synapse.runtime.contracts import RuntimeResponse

_MAX_RESPONSE_BYTES = 512 * 1024
_ALLOWED_PROVIDERS = {"lmstudio", "ollama"}
_ROOT_KEYS = {
    "schema_version",
    "seed_hash",
    "summary",
    "idea_expansion",
    "questions",
    "unresolved",
    "next_action",
    "structure",
}


class GuidedAnalysisCandidateError(ValueError):
    """Raised when a provider response cannot become a verified Candidate."""


def _text(value: Any, label: str, *, limit: int = 4_000) -> str:
    if not isinstance(value, str):
        raise GuidedAnalysisCandidateError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if not result:
        raise GuidedAnalysisCandidateError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise GuidedAnalysisCandidateError(f"{label}이(가) 너무 깁니다.")
    return result


def _json_payload(text: str) -> Mapping[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise GuidedAnalysisCandidateError("모델 응답이 비어 있습니다.")
    if len(raw.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise GuidedAnalysisCandidateError("모델 응답이 너무 큽니다.")
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"}:
            raise GuidedAnalysisCandidateError("모델 응답 JSON 코드블록 형식이 올바르지 않습니다.")
        if lines[-1].strip() != "```":
            raise GuidedAnalysisCandidateError("모델 응답 JSON 코드블록이 닫히지 않았습니다.")
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
            raise GuidedAnalysisCandidateError(
                "모델 응답에서 유일한 guided analysis JSON 객체를 찾을 수 없습니다."
            ) from exc
        payload = candidates[0]
    if not isinstance(payload, Mapping):
        raise GuidedAnalysisCandidateError("모델 응답 최상위 값은 JSON 객체여야 합니다.")
    unknown = set(payload) - _ROOT_KEYS
    missing = _ROOT_KEYS - set(payload)
    if unknown:
        raise GuidedAnalysisCandidateError(f"analysis response에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    if missing:
        raise GuidedAnalysisCandidateError(f"analysis response에 필수 필드가 없습니다: {sorted(missing)}")
    return payload


def _questions_record(value: Any) -> str:
    return canonical_json(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedAnalysisCandidate:
    """A provider response normalized into two linked PROPOSED proposals."""

    id: str
    seed_id: str
    seed_hash: str
    provider: str
    model: str
    request_id: str
    prompt_version: str
    response_hash: str
    expansion: IdeaExpansionProposal
    structure: StructureMapProposal
    owner: str = "human-ui"
    source: str = "guided-ui"
    status: str = "PROPOSED"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.seed_id.strip() or not self.seed_hash.strip():
            raise GuidedAnalysisCandidateError("GuidedAnalysisCandidate 식별자와 seed hash가 필요합니다.")
        provider = self.provider.strip().lower()
        if provider not in _ALLOWED_PROVIDERS:
            raise GuidedAnalysisCandidateError("지원하지 않는 local provider입니다.")
        object.__setattr__(self, "provider", provider)
        _text(self.model, "model", limit=240)
        _text(self.request_id, "request_id", limit=240)
        _text(self.prompt_version, "prompt_version", limit=120)
        _text(self.response_hash, "response_hash", limit=120)
        _text(self.owner, "owner", limit=160)
        _text(self.source, "source", limit=160)
        if self.status != "PROPOSED":
            raise GuidedAnalysisCandidateError("analysis Candidate는 PROPOSED로 시작해야 합니다.")
        if self.canonical_mutation:
            raise GuidedAnalysisCandidateError("analysis Candidate는 Canonical을 변경할 수 없습니다.")
        if self.expansion.seed_hash != self.seed_hash or self.structure.seed_hash != self.seed_hash:
            raise GuidedAnalysisCandidateError("analysis Candidate의 seed hash가 일치하지 않습니다.")
        if self.structure.expansion_id != self.expansion.id:
            raise GuidedAnalysisCandidateError("StructureMap이 다른 IdeaExpansion을 참조합니다.")
        if _questions_record([item.to_record() for item in self.expansion.questions]) != _questions_record(
            [item.to_record() for item in self.structure.questions]
        ):
            raise GuidedAnalysisCandidateError("IdeaExpansion과 StructureMap의 질문이 일치하지 않습니다.")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seed_id": self.seed_id,
            "seed_hash": self.seed_hash,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "prompt_version": self.prompt_version,
            "response_hash": self.response_hash,
            "owner": self.owner,
            "source": self.source,
            "status": self.status,
            "provenance": dict(self.provenance),
            "expansion": self.expansion.to_record(),
            "structure": self.structure.to_record(),
            "canonical_mutation": False,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedAnalysisVerification:
    """Deterministic verification receipt for a GuidedAnalysisCandidate."""

    id: str
    candidate_id: str
    passed: bool
    checks: Mapping[str, bool]
    errors: tuple[str, ...] = ()
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.candidate_id.strip():
            raise GuidedAnalysisCandidateError("analysis verification 식별자가 필요합니다.")
        if self.canonical_mutation:
            raise GuidedAnalysisCandidateError("analysis verification은 Canonical을 변경할 수 없습니다.")
        normalized = {str(key): bool(value) for key, value in self.checks.items()}
        errors = tuple(dict.fromkeys(str(error).strip() for error in self.errors if str(error).strip()))
        object.__setattr__(self, "checks", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "errors", errors)
        if self.passed != (not errors and all(normalized.values())):
            raise GuidedAnalysisCandidateError("analysis verification passed 값이 check/error와 일치하지 않습니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "passed": self.passed,
            "checks": dict(self.checks),
            "errors": list(self.errors),
            "canonical_mutation": False,
        }


def _bounded_guided_analysis_json_schema(seed_hash: str, node_ids: tuple[str, ...]) -> dict[str, Any]:
    """Build the LM Studio grammar schema from the deterministic core allowlists."""

    string_array = {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 80},
        "maxItems": 3,
    }
    required_string_array = {**string_array, "minItems": 1}
    question = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80},
            "question": {"type": "string", "minLength": 1, "maxLength": 120},
            "why_needed": {"type": "string", "minLength": 1, "maxLength": 120},
            "affected_nodes": {
                "type": "array", "items": {"type": "string", "enum": list(node_ids)}, "maxItems": 4,
            },
            "suggestions": {
                "type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 3,
            },
            "blocking": {"type": "boolean"},
        },
        "required": ["id", "question", "why_needed", "affected_nodes", "suggestions", "blocking"],
        "additionalProperties": False,
    }
    node = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": list(node_ids)},
            "kind": {"type": "string", "enum": sorted(NODE_KINDS)},
            "title": {"type": "string", "minLength": 1, "maxLength": 80},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 120},
            "required": {"type": "boolean"},
            "source_refs": {
                "type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 4,
            },
            "completion_criteria": {"type": "string", "minLength": 1, "maxLength": 120},
            "status": {"type": "string", "enum": sorted(NODE_STATUSES)},
            "artifact_role": {"type": "string", "enum": sorted(ARTIFACT_ROLE_PATHS)},
        },
        "required": [
            "id", "kind", "title", "purpose", "required", "source_refs",
            "completion_criteria", "status", "artifact_role",
        ],
        "additionalProperties": False,
    }
    edge = {
        "type": "object",
        "properties": {
            "from": {"type": "string", "enum": list(node_ids)},
            "to": {"type": "string", "enum": list(node_ids)},
            "relation": {"type": "string", "enum": sorted(EDGE_RELATIONS)},
        },
        "required": ["from", "to", "relation"],
        "additionalProperties": False,
    }
    work_unit = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": ["work:first"]},
            "title": {"type": "string", "minLength": 1, "maxLength": 80},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 120},
            "prerequisites": {
                "type": "array", "items": {"type": "string", "enum": list(node_ids)}, "maxItems": 4,
            },
            "source_node_ids": {
                "type": "array", "items": {"type": "string", "enum": list(node_ids)}, "maxItems": 4,
            },
            "artifact_roles": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(ARTIFACT_ROLE_PATHS)},
                "maxItems": 4,
            },
            "completion_criteria": {"type": "string", "minLength": 1, "maxLength": 120},
            "downstream_impacts": {
                "type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 4,
            },
            "status": {"type": "string", "enum": sorted(WORK_STATUSES)},
        },
        "required": [
            "id", "title", "purpose", "prerequisites", "source_node_ids", "artifact_roles",
            "completion_criteria", "downstream_impacts", "status",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "enum": ["guided.analysis.v1"]},
            "seed_hash": {"type": "string", "enum": [seed_hash]},
            "summary": {"type": "string", "minLength": 1, "maxLength": 320},
            "idea_expansion": {
                "type": "object",
                "properties": {
                    "intended_outcome": {"type": "string", "minLength": 1, "maxLength": 320},
                    "audience": string_array,
                    "scope_in": string_array,
                    "scope_out": string_array,
                    "constraints": string_array,
                    "success_criteria": required_string_array,
                    "concepts": string_array,
                    "workflows": string_array,
                    "assumptions": string_array,
                },
                "required": [
                    "intended_outcome", "audience", "scope_in", "scope_out", "constraints",
                    "success_criteria", "concepts", "workflows", "assumptions",
                ],
                "additionalProperties": False,
            },
            "questions": {"type": "array", "items": question, "maxItems": 2},
            "unresolved": string_array,
            "next_action": {
                "type": "object",
                "properties": {"kind": {"type": "string", "minLength": 1, "maxLength": 80}},
                "required": ["kind"],
                "additionalProperties": False,
            },
            "structure": {
                "type": "object",
                "properties": {
                    "nodes": {
                        "type": "array",
                        "items": node,
                        "minItems": len(node_ids),
                        "maxItems": len(node_ids),
                        "uniqueItems": True,
                    },
                    "edges": {"type": "array", "items": edge, "maxItems": 12},
                    "work_units": {
                        "type": "array", "items": work_unit, "minItems": 1, "maxItems": 1,
                    },
                },
                "required": ["nodes", "edges", "work_units"],
                "additionalProperties": False,
            },
        },
        "required": [
            "schema_version", "seed_hash", "summary", "idea_expansion", "questions",
            "unresolved", "next_action", "structure",
        ],
        "additionalProperties": False,
    }



def build_guided_analysis_json_schema(
    seed_hash: str, *, node_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """The one place the analyze response_format schema is built.

    Every key set and enum here is imported from the modules that actually
    validate the response (_ROOT_KEYS above, idea_expansion._EXPANSION_KEYS/
    _QUESTION_KEYS, structure_map's NODE_RESPONSE_KEYS/EDGE_RESPONSE_KEYS/
    WORK_UNIT_RESPONSE_KEYS and their *_KINDS/*_STATUSES/EDGE_RELATIONS
    enums), so the schema cannot drift from what the parsers accept.

    Deliberately omits ``minLength``/``uniqueItems`` string constraints: a
    live probe on 2026-09-03 measured LM Studio's grammar compilation going
    from ~70s to ~6.5 minutes for this schema once those were added, for the
    same task. Type/required/enum constraints alone already eliminate the
    non-JSON, missing-field, wrong-type, and bad-enum failure classes; an
    occasional empty string inside an otherwise well-shaped response is left
    to the existing strict parsers below, which reject it as MISSING rather
    than accept it -- the same tradeoff already accepted for slot-draft.
    """

    if node_ids is not None:
        return _bounded_guided_analysis_json_schema(seed_hash, node_ids)

    string_array = {"type": "array", "items": {"type": "string"}}
    expansion_properties = {"intended_outcome": {"type": "string"}}
    for key in _EXPANSION_KEYS - {"intended_outcome"}:
        expansion_properties[key] = string_array
    return {
        "type": "object",
        "properties": {
            "schema_version": {"const": "guided.analysis.v1"},
            "seed_hash": {"const": seed_hash},
            "summary": {"type": "string"},
            "idea_expansion": {
                "type": "object",
                "properties": expansion_properties,
                "required": sorted(_EXPANSION_KEYS),
                "additionalProperties": False,
            },
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "question": {"type": "string"},
                        "why_needed": {"type": "string"},
                        "affected_nodes": string_array,
                        "suggestions": string_array,
                        "blocking": {"type": "boolean"},
                    },
                    "required": sorted(_QUESTION_KEYS),
                    "additionalProperties": False,
                },
            },
            "unresolved": string_array,
            "next_action": {
                "type": "object",
                "properties": {"type": {"type": "string"}},
                "required": ["type"],
                "additionalProperties": False,
            },
            "structure": {
                "type": "object",
                "properties": {
                    "nodes": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "kind": {"enum": sorted(NODE_KINDS)},
                                "title": {"type": "string"},
                                "purpose": {"type": "string"},
                                "required": {"type": "boolean"},
                                "source_refs": string_array,
                                "completion_criteria": {"type": "string"},
                                "status": {"enum": sorted(NODE_STATUSES)},
                                "artifact_role": {
                                    "type": ["string", "null"],
                                    "enum": [*sorted(ARTIFACT_ROLE_PATHS), None],
                                },
                            },
                            "required": sorted(NODE_RESPONSE_KEYS),
                            "additionalProperties": False,
                        },
                    },
                    "edges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                                "relation": {"enum": sorted(EDGE_RELATIONS)},
                            },
                            "required": sorted(EDGE_RESPONSE_KEYS),
                            "additionalProperties": False,
                        },
                    },
                    "work_units": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "purpose": {"type": "string"},
                                "prerequisites": string_array,
                                "source_node_ids": string_array,
                                "artifact_roles": string_array,
                                "completion_criteria": {"type": "string"},
                                "downstream_impacts": string_array,
                                "status": {"enum": sorted(WORK_STATUSES)},
                            },
                            "required": sorted(WORK_UNIT_RESPONSE_KEYS),
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["nodes", "edges", "work_units"],
                "additionalProperties": False,
            },
        },
        "required": sorted(_ROOT_KEYS),
        "additionalProperties": False,
    }


def build_guided_analysis_candidate(
    seed: IdeaSeed,
    runtime_response: RuntimeResponse,
    *,
    expansion_revision: int = 1,
    structure_revision: int = 1,
    parent_map_hash: str | None = None,
    prompt_version: str = "guided.analysis.v1",
    owner: str = "human-ui",
    source: str = "guided-ui",
) -> GuidedAnalysisCandidate:
    """Parse one strict provider response and build a linked Candidate."""

    provider = runtime_response.provider.strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise GuidedAnalysisCandidateError("analysis는 LM Studio 또는 Ollama만 지원합니다.")
    if expansion_revision < 1 or structure_revision < 1:
        raise GuidedAnalysisCandidateError("revision은 1 이상이어야 합니다.")
    if structure_revision == 1 and parent_map_hash is not None:
        raise GuidedAnalysisCandidateError("revision 1에는 parent_map_hash를 둘 수 없습니다.")
    if structure_revision > 1 and not str(parent_map_hash or "").strip():
        raise GuidedAnalysisCandidateError("revision 2 이상에는 parent_map_hash가 필요합니다.")
    payload = _json_payload(runtime_response.text)
    response_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    provenance = {
        "provider": provider,
        "model": runtime_response.model,
        "request_id": runtime_response.request_id,
        "prompt_version": _text(prompt_version, "prompt_version", limit=120),
        "response_hash": sha256_text(runtime_response.text.strip()),
        "response_finish_reason": runtime_response.finish_reason,
    }
    try:
        expansion = parse_idea_expansion_response(
            response_text,
            expected_seed_hash=seed.raw_hash,
            revision=expansion_revision,
            owner=owner,
            source=source,
            provenance=provenance,
        )
        structure_payload = {
            key: payload[key]
            for key in ("schema_version", "seed_hash", "structure", "questions", "unresolved", "next_action")
        }
        structure = parse_structure_map_response(
            json.dumps(structure_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            expected_seed_hash=seed.raw_hash,
            expansion_id=expansion.id,
            revision=structure_revision,
            parent_map_hash=parent_map_hash,
            preset_hint=seed.preset_hint,
            owner=owner,
            source=source,
            provenance=provenance,
        )
    except (IdeaExpansionError, StructureMapError) as exc:
        raise GuidedAnalysisCandidateError(str(exc)) from exc
    candidate_seed = {
        "seed_id": seed.id,
        "seed_hash": seed.raw_hash,
        "provider": provider,
        "model": runtime_response.model,
        "request_id": runtime_response.request_id,
        "prompt_version": prompt_version,
        "expansion_hash": expansion.expansion_hash,
        "structure_hash": structure.map_hash,
        "expansion_revision": expansion_revision,
        "structure_revision": structure_revision,
        "parent_map_hash": parent_map_hash,
    }
    candidate_id = f"guided-analysis:{hashlib.sha256(canonical_json(candidate_seed).encode('utf-8')).hexdigest()}"
    return GuidedAnalysisCandidate(
        id=candidate_id,
        seed_id=seed.id,
        seed_hash=seed.raw_hash,
        provider=provider,
        model=runtime_response.model,
        request_id=runtime_response.request_id,
        prompt_version=prompt_version,
        response_hash=sha256_text(runtime_response.text.strip()),
        expansion=expansion,
        structure=structure,
        owner=owner,
        source=source,
        provenance=provenance,
    )


def verify_guided_analysis_candidate(
    candidate: GuidedAnalysisCandidate,
    seed: IdeaSeed,
) -> GuidedAnalysisVerification:
    """Verify linked proposal invariants without provider or filesystem access."""

    checks = {
        "status_proposed": candidate.status == "PROPOSED",
        "provider_allowlisted": candidate.provider in _ALLOWED_PROVIDERS,
        "request_id_present": bool(candidate.request_id.strip()),
        "model_present": bool(candidate.model.strip()),
        "seed_id_matches": candidate.seed_id == seed.id,
        "seed_hash_matches": candidate.seed_hash == seed.raw_hash,
        "expansion_seed_matches": candidate.expansion.seed_hash == seed.raw_hash,
        "structure_seed_matches": candidate.structure.seed_hash == seed.raw_hash,
        "expansion_link_matches": candidate.structure.expansion_id == candidate.expansion.id,
        "questions_aligned": _questions_record([item.to_record() for item in candidate.expansion.questions])
        == _questions_record([item.to_record() for item in candidate.structure.questions]),
        "canonical_mutation_false": candidate.canonical_mutation is False,
        "response_hash_format": candidate.response_hash.startswith("sha256:"),
        "provenance_provider_matches": candidate.provenance.get("provider") == candidate.provider,
        "provenance_present": all(
            bool(str(candidate.provenance.get(key, "")).strip())
            for key in ("provider", "model", "request_id", "prompt_version", "response_hash")
        ),
    }
    errors = tuple(key for key, passed in checks.items() if not passed)
    receipt_seed = {"candidate_id": candidate.id, "checks": checks, "errors": errors}
    receipt_id = f"guided-analysis-verification:{canonical_hash(receipt_seed).removeprefix('sha256:')}"
    return GuidedAnalysisVerification(
        id=receipt_id,
        candidate_id=candidate.id,
        passed=not errors and all(checks.values()),
        checks=checks,
        errors=errors,
    )


__all__ = [
    "GuidedAnalysisCandidate",
    "GuidedAnalysisCandidateError",
    "GuidedAnalysisVerification",
    "build_guided_analysis_candidate",
    "build_guided_analysis_json_schema",
    "verify_guided_analysis_candidate",
]
