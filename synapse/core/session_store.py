"""Explicit, non-authoritative persistence for Guided session resume points.

The store is intentionally smaller than a browser draft.  It keeps the raw
idea, user-entered answers, deterministic plan coordinates, and references to
derived artifacts, but never stores model response bodies or workspace file
contents.  Saving is an explicit operation; loading is read-only and always
revalidates the snapshot before exposing a resume projection.

This module never writes Canonical State or a workspace, calls a model, or
uses the network.  A checksum detects accidental edits and ordinary
tampering, but is not an authentication mechanism.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from synapse.core.guided_detail import GuidedDetailError, GuidedDetailPlan, build_guided_detail_plan
from synapse.core.idea_session import GUIDED_STAGES, IdeaSeed, create_idea_seed, sha256_text

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_KIND = "guided_session_snapshot"
CHECKSUM_ALGORITHM = "sha256"
DEFAULT_SNAPSHOT_FILENAME = "guided-session.json"

_SNAPSHOT_STATUSES = {"SAVED", "LOADED_UNVERIFIED", "RESUMED", "STALE_RESET"}
_REF_KINDS = {
    "analysis_candidate",
    "work_unit_candidate",
    "workspace_candidate",
    "workspace_proposal",
    "expansion",
    "structure_map",
    "candidate_ref",
}
_WORKSPACE_REF_KINDS = {"workspace_candidate", "workspace_proposal"}
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_KEYS = {
    "session_id",
    "stage",
    "seed",
    "category_ids",
    "detail_plan_ref",
    "detail_answers",
    "answers",
    "expansion_ref",
    "structure_map_ref",
    "candidate_refs",
    "workspace_binding",
    "analysis_context_ref",
}
_REF_KEYS = {
    "id",
    "kind",
    "seed_hash",
    "plan_id",
    "detail_plan_id",
    "revision",
    "map_hash",
    "workspace_path",
    "base_hashes",
}


class SessionStoreError(ValueError):
    """Raised when a Guided session snapshot cannot be trusted or written."""

    def __init__(self, code: str, message: str, *, path: str | Path | None = None) -> None:
        self.code = code
        self.path = str(path) if path is not None else None
        location = f" [{self.path}]" if self.path else ""
        super().__init__(f"{code}: {message}{location}")


SessionPersistenceError = SessionStoreError


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _mapping(value: Any, label: str, *, code: str = "SS-E02") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionStoreError(code, f"{label}는 object여야 합니다.")
    return value


def _text(value: Any, label: str, *, limit: int = 4_000, code: str = "SS-E02") -> str:
    if not isinstance(value, str):
        raise SessionStoreError(code, f"{label}은(는) 문자열이어야 합니다.")
    result = value.strip()
    if not result:
        raise SessionStoreError(code, f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise SessionStoreError(code, f"{label}이(가) 너무 깁니다.")
    return result


def _raw_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise SessionStoreError("SS-E02", f"{label}은(는) 문자열이어야 합니다.")
    if not value.strip():
        raise SessionStoreError("SS-E02", f"{label}은(는) 비어 있을 수 없습니다.")
    if len(value) > 20_000:
        raise SessionStoreError("SS-E02", f"{label}이(가) 너무 깁니다.")
    return value


def _record(value: Any, label: str) -> dict[str, Any]:
    if hasattr(value, "to_record"):
        value = value.to_record()
    mapping = _mapping(value, label)
    return {str(key): item for key, item in mapping.items()}


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SessionStoreError("SS-E02", f"{label}은(는) 1 이상의 정수여야 합니다.")
    return value


def _safe_relative_path(value: Any, label: str) -> str:
    result = _text(value, label, limit=500)
    if "\\" in result:
        raise SessionStoreError("SS-E02", f"{label}은(는) POSIX 경로여야 합니다.")
    candidate = PurePosixPath(result)
    if candidate.is_absolute() or ":" in candidate.parts[0] or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise SessionStoreError("SS-E02", f"{label}이(가) 안전한 상대 경로가 아닙니다.")
    return result


def _hash_or_none(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise SessionStoreError("SS-E02", f"{label}은(는) sha256 hex 값이어야 합니다.")
    return value


def _absolute_path(value: Any, label: str) -> str:
    raw = _text(value, label, limit=2_000)
    try:
        return str(Path(raw).expanduser().resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        raise SessionStoreError("SS-E02", f"{label}을(를) 해석할 수 없습니다: {exc}") from exc


def _safe_filename(value: str | Path | None) -> str:
    raw = str(value or DEFAULT_SNAPSHOT_FILENAME).strip()
    if (
        not raw
        or raw in {".", ".."}
        or "/" in raw
        or "\\" in raw
        or ":" in raw
        or len(raw) > 160
        or Path(raw).name != raw
    ):
        raise SessionStoreError("SS-E01", "snapshot filename은 단일 안전한 파일명이어야 합니다.")
    return raw


def _directory(value: str | Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise SessionStoreError("SS-E01", "사용자 지정 snapshot directory가 필요합니다.")
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SessionStoreError("SS-E01", f"snapshot directory를 해석할 수 없습니다: {exc}") from exc


def _snapshot_path(value: str | Path, filename: str | Path | None = None) -> Path:
    if filename is None:
        try:
            return Path(value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SessionStoreError("SS-E01", f"snapshot path를 해석할 수 없습니다: {exc}") from exc
    return _directory(value) / _safe_filename(filename)


def _text_map(value: Any, label: str) -> dict[str, str]:
    mapping = _mapping(value, label)
    result: dict[str, str] = {}
    for raw_key, raw_value in mapping.items():
        key = _text(raw_key, f"{label} key", limit=240)
        result[key] = _text(raw_value, f"{label}[{key}]", limit=4_000)
    return dict(sorted(result.items()))


def _detail_answers(value: Any) -> dict[str, Any]:
    mapping = _mapping(value, "detail_answers")
    result: dict[str, Any] = {}
    for raw_key, raw_value in mapping.items():
        key = _text(raw_key, "detail_answers key", limit=240)
        if isinstance(raw_value, str):
            if len(raw_value) > 4_000:
                raise SessionStoreError("SS-E02", f"detail_answers[{key}]이(가) 너무 깁니다.")
            result[key] = raw_value
            continue
        answer = _mapping(raw_value, f"detail_answers[{key}]")
        unknown = set(answer) - {"value", "status", "source_refs"}
        if unknown:
            raise SessionStoreError(
                "SS-E02",
                f"detail_answers[{key}]에 알 수 없는 필드가 있습니다: {sorted(unknown)}",
            )
        raw_answer = answer.get("value", "")
        if not isinstance(raw_answer, str) or len(raw_answer) > 4_000:
            raise SessionStoreError("SS-E02", f"detail_answers[{key}].value가 올바르지 않습니다.")
        status = str(answer.get("status", "ACCEPTED")).strip().upper()
        if status not in {"ACCEPTED", "PROPOSED", "DEFERRED"}:
            raise SessionStoreError("SS-E02", f"detail_answers[{key}].status가 올바르지 않습니다.")
        refs = answer.get("source_refs", ["user_answer"])
        if not isinstance(refs, list | tuple) or any(
            not isinstance(item, str) or not item.strip() for item in refs
        ):
            raise SessionStoreError("SS-E02", f"detail_answers[{key}].source_refs가 올바르지 않습니다.")
        result[key] = {
            "value": raw_answer,
            "status": status,
            "source_refs": list(dict.fromkeys(item.strip() for item in refs)),
        }
    return dict(sorted(result.items()))


def _category_ids(value: Any, preset_hint: str) -> tuple[str, ...]:
    raw = value if value is not None else [preset_hint]
    if not isinstance(raw, list | tuple):
        raise SessionStoreError("SS-E02", "category_ids는 문자열 배열이어야 합니다.")
    result = ["general"]
    for item in raw:
        category = _text(item, "category_id", limit=80).lower()
        if category not in result:
            result.append(category)
    return tuple(result)


def _validate_seed(seed_value: Any, *, for_save: bool) -> tuple[dict[str, Any], IdeaSeed, bool]:
    seed = _record(seed_value, "seed")
    raw_text = _raw_text(seed.get("raw_text"), "seed.raw_text")
    project_name = _text(seed.get("project_name"), "seed.project_name", limit=160)
    preset_hint = _text(seed.get("preset_hint", "general"), "seed.preset_hint", limit=64)
    expected_hash = sha256_text(raw_text)
    supplied_hash = seed.get("raw_hash")
    if not isinstance(supplied_hash, str) or not supplied_hash:
        if for_save:
            supplied_hash = expected_hash
        else:
            raise SessionStoreError("SS-E02", "snapshot seed.raw_hash가 필요합니다.")
    seed_hash_ok = supplied_hash == expected_hash
    if for_save and not seed_hash_ok:
        raise SessionStoreError("SS-E04", "저장할 seed hash가 raw_text와 일치하지 않습니다.")
    try:
        canonical_seed = create_idea_seed(
            project_name,
            raw_text,
            preset_hint=preset_hint,
            owner="session-store",
            source="guided-session-snapshot",
        )
    except ValueError as exc:
        raise SessionStoreError("SS-E02", f"seed를 검증할 수 없습니다: {exc}") from exc
    seed_id_ok = not seed.get("id") or str(seed["id"]) == canonical_seed.id
    if for_save and not seed_id_ok and str(seed.get("id", "")) != "local-seed":
        raise SessionStoreError("SS-E04", "저장할 seed id가 결정론적 seed id와 다릅니다.")
    payload = {
        "id": canonical_seed.id,
        "project_name": canonical_seed.project_name,
        "raw_text": raw_text,
        "preset_hint": canonical_seed.preset_hint,
        "raw_hash": expected_hash,
    }
    return payload, canonical_seed, seed_hash_ok and seed_id_ok


def _base_hashes(value: Any, label: str) -> dict[str, str | None]:
    if value is None:
        return {}
    mapping = _mapping(value, label)
    result = {
        _safe_relative_path(path, f"{label} path"): _hash_or_none(digest, f"{label}[{path}]")
        for path, digest in mapping.items()
    }
    return dict(sorted(result.items()))


def _reference(
    value: Any,
    *,
    kind: str,
    base_hashes: Mapping[str, Any] | None = None,
    required: bool = False,
) -> dict[str, Any] | None:
    record = _record(value, f"{kind} reference")
    identifier = record.get("id")
    if not identifier:
        if required:
            raise SessionStoreError("SS-E02", f"{kind} reference id가 필요합니다.")
        return None
    if kind not in _REF_KINDS:
        raise SessionStoreError("SS-E02", f"지원하지 않는 reference kind입니다: {kind}")
    result: dict[str, Any] = {"id": _text(identifier, f"{kind}.id", limit=320), "kind": kind}
    for field_name in ("seed_hash", "plan_id", "detail_plan_id", "map_hash"):
        if record.get(field_name) not in (None, ""):
            result[field_name] = _text(record[field_name], f"{kind}.{field_name}", limit=320)
    if record.get("revision") is not None:
        result["revision"] = _positive_int(record["revision"], f"{kind}.revision")
    workspace_path = record.get("workspace_path")
    if workspace_path:
        result["workspace_path"] = _absolute_path(workspace_path, f"{kind}.workspace_path")
    if base_hashes is None:
        base_hashes = record.get("base_hashes")
        changes = record.get("changes")
        if base_hashes is None and isinstance(changes, Mapping):
            base_hashes = changes.get("base_hashes")
    result["base_hashes"] = _base_hashes(base_hashes, f"{kind}.base_hashes")
    return result


def _workspace_candidate_reference(value: Any) -> dict[str, Any] | None:
    record = _record(value, "workspace_candidate")
    candidate = record.get("candidate")
    if isinstance(candidate, Mapping):
        record = dict(candidate)
    return _reference(record, kind="workspace_candidate")


def _workspace_proposal_reference(value: Any) -> dict[str, Any] | None:
    record = _record(value, "workspace_proposal")
    proposal_set = record.get("proposal_set", record)
    proposal_set = _mapping(proposal_set, "workspace_proposal.proposal_set")
    hashes: dict[str, str | None] = {}
    proposals = proposal_set.get("proposals", [])
    if not isinstance(proposals, list | tuple):
        raise SessionStoreError("SS-E02", "workspace proposal proposals가 올바르지 않습니다.")
    for proposal_value in proposals:
        proposal = _mapping(proposal_value, "workspace proposal item")
        path = _safe_relative_path(proposal.get("path"), "workspace proposal path")
        hashes[path] = _hash_or_none(proposal.get("base_sha256"), f"workspace proposal[{path}].base_sha256")
    return _reference(
        proposal_set,
        kind="workspace_proposal",
        base_hashes=hashes,
    )


def _candidate_references(session: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    references: list[dict[str, Any]] = []
    explicit = session.get("candidate_refs", [])
    if explicit is None:
        explicit = []
    if not isinstance(explicit, list | tuple):
        raise SessionStoreError("SS-E02", "candidate_refs는 배열이어야 합니다.")
    for item in explicit:
        entry = _mapping(item, "candidate_refs item")
        kind = _text(entry.get("kind"), "candidate_refs.kind", limit=80)
        reference = _reference(entry, kind=kind, required=True)
        if reference is not None:
            references.append(reference)

    for key, kind in (
        ("candidate", "analysis_candidate"),
        ("work_unit_candidate", "work_unit_candidate"),
    ):
        value = session.get(key)
        if value:
            reference = _reference(value, kind=kind)
            if reference is not None:
                references.append(reference)
    if session.get("workspace_candidate"):
        reference = _workspace_candidate_reference(session["workspace_candidate"])
        if reference is not None:
            references.append(reference)
    if session.get("workspace_proposal"):
        reference = _workspace_proposal_reference(session["workspace_proposal"])
        if reference is not None:
            references.append(reference)

    deduped: dict[str, dict[str, Any]] = {}
    for reference in references:
        identifier = reference["id"]
        previous = deduped.get(identifier)
        if previous is not None and _canonical(previous) != _canonical(reference):
            raise SessionStoreError("SS-E03", f"candidate reference id가 서로 다릅니다: {identifier}")
        deduped[identifier] = reference
    return tuple(deduped[key] for key in sorted(deduped))


def _workspace_binding(session: Mapping[str, Any]) -> dict[str, str] | None:
    value = session.get("workspace_binding")
    if isinstance(value, Mapping):
        path = value.get("path") or value.get("workspace_path")
    else:
        path = value
    if not path:
        path = session.get("workspace_path")
    if not path and isinstance(session.get("workspace_snapshot"), Mapping):
        path = session["workspace_snapshot"].get("workspace_path")
    if not path and isinstance(session.get("workspace_proposal"), Mapping):
        proposal = session["workspace_proposal"].get("proposal_set", session["workspace_proposal"])
        if isinstance(proposal, Mapping):
            path = proposal.get("workspace_path")
    if not path:
        return None
    return {"path": _absolute_path(path, "workspace_binding.path")}


def _analysis_context_reference(session: Mapping[str, Any]) -> dict[str, Any] | None:
    value = session.get("analysis_context")
    if not value:
        return None
    context = _mapping(value, "analysis_context")
    result: dict[str, Any] = {}
    note = context.get("note", "")
    if note:
        if not isinstance(note, str):
            raise SessionStoreError("SS-E02", "analysis_context.note가 올바르지 않습니다.")
        result["note_sha256"] = hashlib.sha256(note.encode("utf-8")).hexdigest()
    files = context.get("files", [])
    if not isinstance(files, list | tuple):
        raise SessionStoreError("SS-E02", "analysis_context.files가 올바르지 않습니다.")
    file_refs: list[dict[str, Any]] = []
    for item in files:
        file = _mapping(item, "analysis_context file")
        name = _text(file.get("name"), "analysis_context file.name", limit=240)
        text = file.get("text")
        if text is not None and not isinstance(text, str):
            raise SessionStoreError("SS-E02", f"analysis_context[{name}].text가 올바르지 않습니다.")
        digest = file.get("sha256")
        if digest is None and text is not None:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not isinstance(digest, str) or _HASH_PATTERN.fullmatch(digest) is None:
            raise SessionStoreError("SS-E02", f"analysis_context[{name}] hash가 필요합니다.")
        size = file.get("size", len(text.encode("utf-8")) if isinstance(text, str) else 0)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SessionStoreError("SS-E02", f"analysis_context[{name}].size가 올바르지 않습니다.")
        file_refs.append({"name": name, "size": size, "sha256": digest})
    if file_refs:
        result["files"] = sorted(file_refs, key=lambda item: item["name"].casefold())
    return result or None


def build_session_snapshot_payload(session: Any) -> dict[str, Any]:
    """Project a browser or Python session into a safe, reference-only payload."""

    record = _record(session, "session")
    session_id = _text(record.get("session_id"), "session_id", limit=320)
    stage = _text(record.get("stage", "WELCOME"), "stage", limit=80)
    if stage not in GUIDED_STAGES:
        raise SessionStoreError("SS-E02", f"지원하지 않는 Guided stage입니다: {stage}")
    seed_payload, seed, _seed_ok = _validate_seed(record.get("seed"), for_save=True)
    categories = _category_ids(record.get("category_ids"), seed.preset_hint)
    answers = _detail_answers(record.get("detail_answers", {}))
    raw_plan = record.get("detail_plan")
    plan_revision = 1
    plan_id = ""
    if raw_plan:
        plan_record = _record(raw_plan, "detail_plan")
        plan_id = _text(plan_record.get("id"), "detail_plan.id", limit=400)
        plan_revision = _positive_int(plan_record.get("revision"), "detail_plan.revision")
        plan_seed_hash = _text(plan_record.get("seed_hash"), "detail_plan.seed_hash", limit=160)
        if plan_seed_hash != seed.raw_hash:
            raise SessionStoreError("SS-E04", "저장할 detail plan의 seed hash가 seed와 다릅니다.")
        plan_categories = _category_ids(plan_record.get("category_ids"), seed.preset_hint)
        if plan_categories != categories:
            raise SessionStoreError("SS-E04", "저장할 detail plan의 category가 현재 session과 다릅니다.")
    try:
        plan = build_guided_detail_plan(
            seed,
            category_ids=categories,
            answers=answers,
            revision=plan_revision,
        )
    except (GuidedDetailError, ValueError) as exc:
        raise SessionStoreError("SS-E02", f"현재 detail plan을 재산출할 수 없습니다: {exc}") from exc
    slot_ids = {slot.id for slot in plan.slots}
    unknown_answers = sorted(set(answers) - slot_ids)
    if unknown_answers:
        raise SessionStoreError("SS-E05", f"현재 detail 규칙에 없는 answer가 있습니다: {unknown_answers}")
    detail_plan_ref = {
        "id": plan_id or plan.id,
        "seed_hash": seed.raw_hash,
        "category_ids": list(plan.category_ids),
        "revision": plan_revision,
    }
    expansion_ref = _reference(record["expansion"], kind="expansion") if record.get("expansion") else None
    structure_ref = _reference(record["structure"], kind="structure_map") if record.get("structure") else None
    return {
        "session_id": session_id,
        "stage": stage,
        "seed": seed_payload,
        "category_ids": list(plan.category_ids),
        "detail_plan_ref": detail_plan_ref,
        "detail_answers": answers,
        "answers": _text_map(record.get("answers", {}), "answers"),
        "expansion_ref": expansion_ref,
        "structure_map_ref": structure_ref,
        "candidate_refs": [dict(item) for item in _candidate_references(record)],
        "workspace_binding": _workspace_binding(record),
        "analysis_context_ref": _analysis_context_reference(record),
    }


@dataclass(frozen=True, slots=True)
class SessionSaveReceipt:
    """Receipt for an explicit snapshot write."""

    path: str
    checksum: str
    status: str = "SAVED"
    canonical_mutation: bool = False
    filesystem_mutation: bool = True
    automatic: bool = False

    def __post_init__(self) -> None:
        if self.status != "SAVED" or _HASH_PATTERN.fullmatch(self.checksum) is None:
            raise SessionStoreError("SS-E02", "snapshot save receipt가 올바르지 않습니다.")
        if self.canonical_mutation or not self.filesystem_mutation or self.automatic:
            raise SessionStoreError("SS-E02", "snapshot save 경계가 잘못되었습니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "checksum_alg": CHECKSUM_ALGORITHM,
            "checksum": self.checksum,
            "canonical_mutation": False,
            "filesystem_mutation": True,
            "automatic": False,
        }


@dataclass(frozen=True, slots=True)
class SessionLoadReceipt:
    """Read-only result after snapshot validation and stale-reference isolation."""

    path: str
    status: str
    payload: Mapping[str, Any]
    resume_projection: Mapping[str, Any]
    checks: Mapping[str, bool]
    stale_refs: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    canonical_mutation: bool = False
    filesystem_mutation: bool = False
    automatic: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"RESUMED", "STALE_RESET"}:
            raise SessionStoreError("SS-E02", f"snapshot load status가 올바르지 않습니다: {self.status}")
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "resume_projection", _freeze(self.resume_projection))
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))
        object.__setattr__(self, "stale_refs", tuple(sorted(set(self.stale_refs))))
        object.__setattr__(self, "errors", tuple(self.errors))
        if self.canonical_mutation or self.filesystem_mutation or self.automatic:
            raise SessionStoreError("SS-E02", "snapshot load 경계가 잘못되었습니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "payload": _thaw(self.payload),
            "resume_projection": _thaw(self.resume_projection),
            "checks": dict(self.checks),
            "stale_refs": list(self.stale_refs),
            "errors": list(self.errors),
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "automatic": False,
        }


def _write_atomic(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SessionStoreError("SS-E06", f"snapshot을 원자적으로 저장할 수 없습니다: {destination}") from exc


def save_guided_session_snapshot(
    session: Any,
    directory: str | Path,
    *,
    filename: str | Path = DEFAULT_SNAPSHOT_FILENAME,
) -> SessionSaveReceipt:
    """Explicitly save a non-authoritative Guided session snapshot."""

    payload = build_session_snapshot_payload(session)
    checksum = _digest(payload)
    document = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": SNAPSHOT_KIND,
        "created_at_iso": datetime.now(UTC).isoformat(),
        "payload": payload,
        "checksum_alg": CHECKSUM_ALGORITHM,
        "checksum": checksum,
    }
    destination = _snapshot_path(directory, filename)
    try:
        _write_atomic(
            destination,
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
        )
    except SessionStoreError:
        raise
    except (TypeError, ValueError) as exc:
        raise SessionStoreError("SS-E06", f"snapshot JSON을 만들 수 없습니다: {destination}") from exc
    return SessionSaveReceipt(path=str(destination), checksum=checksum)


def _read_document(source: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SessionStoreError("SS-E01", f"snapshot을 읽을 수 없습니다: {source}") from exc
    document = _mapping(document, "snapshot envelope")
    required = {"schema_version", "kind", "created_at_iso", "payload", "checksum_alg", "checksum"}
    if set(document) != required:
        raise SessionStoreError("SS-E02", "snapshot envelope 필드가 정확하지 않습니다.", path=source)
    if document.get("schema_version") != SNAPSHOT_SCHEMA_VERSION or document.get("kind") != SNAPSHOT_KIND:
        raise SessionStoreError("SS-E02", "지원하지 않는 snapshot schema 또는 kind입니다.", path=source)
    if not isinstance(document.get("created_at_iso"), str) or not document["created_at_iso"].strip():
        raise SessionStoreError("SS-E02", "snapshot created_at_iso가 필요합니다.", path=source)
    if document.get("checksum_alg") != CHECKSUM_ALGORITHM:
        raise SessionStoreError("SS-E02", "지원하지 않는 snapshot checksum algorithm입니다.", path=source)
    checksum = document.get("checksum")
    if not isinstance(checksum, str) or _HASH_PATTERN.fullmatch(checksum) is None:
        raise SessionStoreError("SS-E03", "snapshot checksum 형식이 잘못되었습니다.", path=source)
    payload = _mapping(document.get("payload"), "snapshot payload")
    if checksum != _digest(payload):
        raise SessionStoreError("SS-E03", "snapshot checksum이 일치하지 않습니다.", path=source)
    return document


def _validate_reference(value: Any, *, expected_kind: str | None = None) -> dict[str, Any]:
    reference = _mapping(value, "candidate reference")
    unknown = set(reference) - _REF_KEYS
    if unknown:
        raise SessionStoreError("SS-E02", f"candidate reference에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    kind = _text(reference.get("kind"), "candidate reference.kind", limit=80)
    if kind not in _REF_KINDS or (expected_kind and kind != expected_kind):
        raise SessionStoreError("SS-E02", f"지원하지 않는 candidate reference kind입니다: {kind}")
    result = _reference(reference, kind=kind, required=True)
    if result is None:
        raise SessionStoreError("SS-E02", "candidate reference가 비어 있습니다.")
    return result


def _validate_loaded_payload(
    payload_value: Any,
) -> tuple[dict[str, Any], IdeaSeed, GuidedDetailPlan, bool, bool, tuple[dict[str, Any], ...]]:
    payload = dict(_mapping(payload_value, "snapshot payload"))
    unknown = set(payload) - _PAYLOAD_KEYS
    if unknown or set(payload) != _PAYLOAD_KEYS:
        raise SessionStoreError("SS-E02", f"snapshot payload 필드가 정확하지 않습니다: {sorted(unknown)}")
    session_id = _text(payload["session_id"], "payload.session_id", limit=320)
    stage = _text(payload["stage"], "payload.stage", limit=80)
    if stage not in GUIDED_STAGES:
        raise SessionStoreError("SS-E02", f"지원하지 않는 snapshot stage입니다: {stage}")
    seed_payload, seed, seed_integrity = _validate_seed(payload["seed"], for_save=False)
    payload["seed"] = seed_payload
    category_values = payload["category_ids"]
    categories = _category_ids(category_values, seed.preset_hint)
    answers = _detail_answers(payload["detail_answers"])
    plan_ref = _mapping(payload["detail_plan_ref"], "detail_plan_ref")
    required_plan_keys = {"id", "seed_hash", "category_ids", "revision"}
    if set(plan_ref) != required_plan_keys:
        raise SessionStoreError("SS-E02", "detail_plan_ref 필드가 정확하지 않습니다.")
    plan_revision = _positive_int(plan_ref["revision"], "detail_plan_ref.revision")
    _text(plan_ref["id"], "detail_plan_ref.id", limit=400)
    plan_seed_hash = _text(plan_ref["seed_hash"], "detail_plan_ref.seed_hash", limit=160)
    seed_integrity = seed_integrity and plan_seed_hash == seed.raw_hash
    ref_categories = _category_ids(plan_ref["category_ids"], seed.preset_hint)
    if ref_categories != categories:
        raise SessionStoreError("SS-E05", "detail_plan_ref와 category_ids가 다릅니다.")
    try:
        plan = build_guided_detail_plan(
            seed,
            category_ids=categories,
            answers=answers,
            revision=plan_revision,
        )
    except (GuidedDetailError, ValueError) as exc:
        raise SessionStoreError("SS-E02", f"snapshot detail plan을 재산출할 수 없습니다: {exc}") from exc
    slot_ids = {slot.id for slot in plan.slots}
    answers_valid = not (set(answers) - slot_ids)
    payload["session_id"] = session_id
    payload["stage"] = stage
    payload["category_ids"] = list(categories)
    payload["detail_answers"] = answers
    payload["answers"] = _text_map(payload["answers"], "answers")
    refs_raw = payload["candidate_refs"]
    if not isinstance(refs_raw, list | tuple):
        raise SessionStoreError("SS-E02", "candidate_refs는 배열이어야 합니다.")
    references = tuple(_validate_reference(item) for item in refs_raw)
    if len({item["id"] for item in references}) != len(references):
        raise SessionStoreError("SS-E03", "candidate reference id가 중복됩니다.")
    for key, expected_kind in (("expansion_ref", "expansion"), ("structure_map_ref", "structure_map")):
        if payload[key] is not None:
            payload[key] = _validate_reference(payload[key], expected_kind=expected_kind)
    binding = payload["workspace_binding"]
    if binding is not None:
        binding_mapping = _mapping(binding, "workspace_binding")
        if set(binding_mapping) != {"path"}:
            raise SessionStoreError("SS-E02", "workspace_binding 필드가 정확하지 않습니다.")
        payload["workspace_binding"] = {"path": _absolute_path(binding_mapping["path"], "workspace_binding.path")}
    context_ref = payload["analysis_context_ref"]
    if context_ref is not None:
        context_mapping = _mapping(context_ref, "analysis_context_ref")
        allowed_context = {"note_sha256", "files"}
        if set(context_mapping) - allowed_context:
            raise SessionStoreError("SS-E02", "analysis_context_ref 필드가 올바르지 않습니다.")
        if "note_sha256" in context_mapping:
            _hash_or_none(context_mapping["note_sha256"], "analysis_context_ref.note_sha256")
        raw_files = context_mapping.get("files", [])
        if not isinstance(raw_files, list | tuple):
            raise SessionStoreError("SS-E02", "analysis_context_ref.files가 올바르지 않습니다.")
        checked_files: list[dict[str, Any]] = []
        for raw_file in raw_files:
            file = _mapping(raw_file, "analysis_context_ref file")
            if set(file) != {"name", "size", "sha256"}:
                raise SessionStoreError("SS-E02", "analysis_context_ref file 필드가 올바르지 않습니다.")
            name = _text(file["name"], "analysis_context_ref file.name", limit=240)
            size = file["size"]
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise SessionStoreError("SS-E02", "analysis_context_ref file.size가 올바르지 않습니다.")
            digest = _hash_or_none(file["sha256"], f"analysis_context_ref[{name}].sha256")
            checked_files.append({"name": name, "size": size, "sha256": digest})
        payload["analysis_context_ref"] = {
            **({"note_sha256": context_mapping["note_sha256"]} if "note_sha256" in context_mapping else {}),
            **({"files": sorted(checked_files, key=lambda item: item["name"].casefold())} if checked_files else {}),
        } or None
    return payload, seed, plan, seed_integrity, answers_valid, references


def _workspace_file_hash(root: Path, relative_path: str) -> str | None:
    candidate = (root / Path(*PurePosixPath(relative_path).parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None


def _reference_staleness(
    references: Sequence[Mapping[str, Any]],
    binding: Mapping[str, Any] | None,
    *,
    current_workspace_path: str | Path | None,
    current_workspace_hashes: Mapping[str, str | None] | None,
) -> tuple[set[str], bool]:
    workspace_refs = [item for item in references if item["kind"] in _WORKSPACE_REF_KINDS]
    if not workspace_refs:
        return set(), True
    current_root: Path | None = None
    if current_workspace_path:
        try:
            current_root = Path(current_workspace_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            current_root = None
    binding_path = None
    if binding:
        binding_path = Path(str(binding.get("path"))).resolve(strict=False)
    path_matches = current_root is not None and binding_path is not None and current_root == binding_path
    if current_workspace_hashes is not None:
        normalized_hashes = _base_hashes(current_workspace_hashes, "current_workspace_hashes")
    else:
        normalized_hashes = None
    stale: set[str] = set()
    for reference in workspace_refs:
        expected_hashes = reference.get("base_hashes", {})
        if not expected_hashes or not path_matches:
            stale.add(str(reference["id"]))
            continue
        for path, expected in expected_hashes.items():
            actual = (
                normalized_hashes.get(path)
                if normalized_hashes is not None
                else _workspace_file_hash(current_root, path)
            )
            if actual != expected:
                stale.add(str(reference["id"]))
                break
    return stale, not stale


def load_guided_session_snapshot(
    path: str | Path,
    *,
    filename: str | Path | None = None,
    current_workspace_path: str | Path | None = None,
    current_workspace_hashes: Mapping[str, str | None] | None = None,
) -> SessionLoadReceipt:
    """Load and revalidate a snapshot without rewriting it or auto-reusing refs."""

    source = _snapshot_path(path, filename)
    document = _read_document(source)
    payload, _seed, plan, seed_integrity, answers_valid, references = _validate_loaded_payload(document["payload"])
    stale_refs, workspace_valid = _reference_staleness(
        references,
        payload.get("workspace_binding"),
        current_workspace_path=current_workspace_path,
        current_workspace_hashes=current_workspace_hashes,
    )
    r1 = seed_integrity
    r2 = answers_valid
    r3 = workspace_valid
    r4 = True
    errors: list[str] = []
    if not r1:
        errors.append("R1: seed raw_text hash가 snapshot seed_hash와 다릅니다.")
    if not r2:
        errors.append("R2: 현재 detail 규칙에 맞지 않는 answer가 있습니다.")
    for reference_id in sorted(stale_refs):
        errors.append(f"R4: workspace reference는 stale이며 자동 재사용하지 않습니다: {reference_id}")
    status = "RESUMED" if r1 and r2 else "STALE_RESET"
    ref_projection = []
    for reference in references:
        ref_status = "STALE" if reference["id"] in stale_refs else "REFERENCE_ONLY"
        ref_projection.append({**reference, "status": ref_status})
    non_reusable_kinds = _WORKSPACE_REF_KINDS | {
        "analysis_candidate",
        "work_unit_candidate",
        "candidate_ref",
    }
    has_non_reusable_ref = any(reference["kind"] in non_reusable_kinds for reference in references)
    safe_stage = (
        payload["stage"]
        if r1 and r2 and not has_non_reusable_ref
        else "IDEA_EXPANSION_READY"
    )
    resume_projection = {
        "session_id": payload["session_id"],
        "stage": safe_stage,
        "original_stage": payload["stage"],
        "seed": dict(payload["seed"]),
        "category_ids": list(payload["category_ids"]),
        "detail_plan": {
            "id": plan.id,
            "revision": plan.revision,
            "seed_hash": plan.seed_hash,
            "readiness": {"can_start": plan.can_start, "required_missing": plan.required_missing},
        },
        "detail_answers": dict(payload["detail_answers"]),
        "answers": dict(payload["answers"]),
        "candidate_refs": ref_projection,
        "workspace_binding": payload["workspace_binding"],
        "resume_allowed": r1 and r2,
        "requires_revalidation": bool(stale_refs) or not r1 or not r2,
        "canonical_mutation": False,
        "filesystem_mutation": False,
        "automatic_reuse": False,
    }
    checks = {
        "R1": r1,
        "R2": r2,
        "R3": r3,
        "R4": r4,
    }
    return SessionLoadReceipt(
        path=str(source),
        status=status,
        payload=payload,
        resume_projection=resume_projection,
        checks=checks,
        stale_refs=tuple(sorted(stale_refs)),
        errors=tuple(errors),
    )


@dataclass(frozen=True, slots=True)
class SessionListEntry:
    """One saved snapshot found while scanning a projects root (read-only, best-effort)."""

    directory: str
    status: str
    project_name: str = ""
    seed_hash: str = ""
    stage: str = ""
    created_at_iso: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"OK", "INVALID"}:
            raise SessionStoreError("SS-E02", f"snapshot list status가 올바르지 않습니다: {self.status}")

    def to_record(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "status": self.status,
            "project_name": self.project_name,
            "seed_hash": self.seed_hash,
            "stage": self.stage,
            "created_at_iso": self.created_at_iso,
            "error": self.error,
        }


def list_saved_guided_sessions(
    root: str | Path,
    *,
    filename: str | Path | None = None,
) -> tuple[SessionListEntry, ...]:
    """Scan root's immediate subdirectories, one saved snapshot per subdirectory.

    Read-only and best-effort: a subdirectory whose snapshot fails to parse or
    revalidate becomes an INVALID entry instead of aborting the whole listing.
    The scan itself is the registry -- no separate index file is kept, so
    there is nothing that can drift from what save_guided_session actually
    wrote.
    """

    safe_name = _safe_filename(filename)
    base = _directory(root)
    if not base.is_dir():
        return ()
    entries: list[SessionListEntry] = []
    for child in sorted((item for item in base.iterdir() if item.is_dir()), key=lambda item: item.name):
        source = child / safe_name
        if not source.is_file():
            continue
        directory = str(child)
        try:
            document = _read_document(source)
            payload = _mapping(document["payload"], "snapshot payload")
            seed = _mapping(payload.get("seed"), "snapshot payload.seed")
            entries.append(
                SessionListEntry(
                    directory=directory,
                    status="OK",
                    project_name=_text(seed.get("project_name"), "seed.project_name", limit=160),
                    seed_hash=_text(seed.get("raw_hash"), "seed.raw_hash", limit=128),
                    stage=_text(payload.get("stage"), "payload.stage", limit=80),
                    created_at_iso=_text(document.get("created_at_iso"), "created_at_iso", limit=64),
                )
            )
        except SessionStoreError as exc:
            entries.append(SessionListEntry(directory=directory, status="INVALID", error=str(exc)))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class SessionDeleteReceipt:
    """Receipt for an explicit, per-directory snapshot delete."""

    directory: str
    status: str
    canonical_mutation: bool = False
    filesystem_mutation: bool = False
    automatic: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"DELETED", "NOT_FOUND"}:
            raise SessionStoreError("SS-E02", f"snapshot delete status가 올바르지 않습니다: {self.status}")
        if self.canonical_mutation or self.automatic:
            raise SessionStoreError("SS-E02", "snapshot delete 경계가 잘못되었습니다.")
        if self.status == "DELETED" and not self.filesystem_mutation:
            raise SessionStoreError("SS-E02", "snapshot delete 경계가 잘못되었습니다.")

    def to_record(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "status": self.status,
            "canonical_mutation": False,
            "filesystem_mutation": self.filesystem_mutation,
            "automatic": False,
        }


def delete_guided_session_snapshots(
    directories: Sequence[str | Path],
    *,
    filename: str | Path | None = None,
) -> tuple[SessionDeleteReceipt, ...]:
    """Explicitly delete one saved-session snapshot file per given directory.

    Deletes only the single well-known snapshot filename inside each given
    directory -- never the directory itself, never an arbitrary path -- so
    this can only remove what save_guided_session_snapshot itself could have
    written.
    """

    safe_name = _safe_filename(filename)
    receipts: list[SessionDeleteReceipt] = []
    for raw_directory in directories:
        directory = _directory(raw_directory)
        source = directory / safe_name
        if not source.is_file():
            receipts.append(SessionDeleteReceipt(directory=str(directory), status="NOT_FOUND"))
            continue
        try:
            source.unlink()
        except OSError as exc:
            raise SessionStoreError("SS-E06", f"snapshot을 삭제할 수 없습니다: {source}") from exc
        receipts.append(SessionDeleteReceipt(directory=str(directory), status="DELETED", filesystem_mutation=True))
    return tuple(receipts)


save_guided_session = save_guided_session_snapshot
load_guided_session = load_guided_session_snapshot


__all__ = [
    "CHECKSUM_ALGORITHM",
    "DEFAULT_SNAPSHOT_FILENAME",
    "SNAPSHOT_KIND",
    "SNAPSHOT_SCHEMA_VERSION",
    "SessionDeleteReceipt",
    "SessionListEntry",
    "SessionLoadReceipt",
    "SessionPersistenceError",
    "SessionSaveReceipt",
    "SessionStoreError",
    "build_session_snapshot_payload",
    "delete_guided_session_snapshots",
    "list_saved_guided_sessions",
    "load_guided_session",
    "load_guided_session_snapshot",
    "save_guided_session",
    "save_guided_session_snapshot",
]
