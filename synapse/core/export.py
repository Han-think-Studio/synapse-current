"""Privacy-gated, deterministic portable ZIP export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from synapse.core.cognitive import CognitiveFrame, build_cognitive_frame
from synapse.core.compiler import ProjectBlueprint
from synapse.core.failure import FailureReport, triage_frame
from synapse.core.guided import GuidedBuildPlan
from synapse.core.ir import SynapseIR

_DENIED_SEGMENTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".pytest_tmp_gate",
        "cache",
        "tmp",
        "temp",
    }
)
_DENIED_NAMES = re.compile(
    r"^(?:\.env(?:\..*)?|auth\.json|credentials?(?:\..*)?|.*(?:secret|token|credential).*|.*\.(?:pem|key|p12|pfx))$",
    re.IGNORECASE,
)
_SECRET_CONTENT = re.compile(
    r"(?:api[_ -]?key|access[_ -]?token|password\s*[:=]|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_TEXT_SUFFIXES = frozenset(
    {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml"}
)


class ExportError(ValueError):
    """Raised when a portable export cannot pass its safety gate."""


@dataclass(frozen=True, slots=True)
class ExportEntry:
    path: str
    size: int
    sha256: str
    role: str
    source_ids: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.path.strip() or self.path.startswith("/"):
            raise ExportError("ExportEntry path가 비어 있거나 절대 경로입니다.")
        if self.size < 0 or len(self.sha256) != 64:
            raise ExportError("ExportEntry size/hash가 잘못되었습니다.")
        if not self.role.strip():
            raise ExportError("ExportEntry role이 비어 있습니다.")
        object.__setattr__(self, "source_ids", tuple(sorted(set(self.source_ids))))
        object.__setattr__(self, "signals", tuple(sorted(set(self.signals))))

    def to_record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "role": self.role,
            "source_ids": list(self.source_ids),
            "signals": list(self.signals),
        }


@dataclass(frozen=True, slots=True)
class PortableExportReport:
    archive: str
    archive_sha256: str
    manifest_sha256: str
    export_level: str
    entries: tuple[ExportEntry, ...]
    warnings: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.warnings

    def to_record(self) -> dict[str, object]:
        return {
            "archive": self.archive,
            "archive_sha256": self.archive_sha256,
            "manifest_sha256": self.manifest_sha256,
            "export_level": self.export_level,
            "entries": [entry.to_record() for entry in self.entries],
            "warnings": list(self.warnings),
            "safe": self.safe,
        }


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalise_relative(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ExportError("export path가 비어 있거나 NUL을 포함합니다.")
    normalised = raw.replace("\\", "/")
    path = PurePosixPath(normalised)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ExportError(f"안전하지 않은 export path입니다: {raw!r}")
    return str(path)


def _denied_reason(relative: str) -> str | None:
    parts = PurePosixPath(relative).parts
    lower_parts = {part.casefold() for part in parts}
    if lower_parts & _DENIED_SEGMENTS:
        return "private_or_generated_directory"
    if _DENIED_NAMES.match(parts[-1]):
        return "secret_like_filename"
    return None


def _content_signals(relative: str, data: bytes) -> tuple[str, ...]:
    if Path(relative).suffix.casefold() not in _TEXT_SUFFIXES:
        return ()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ()
    signals: list[str] = []
    for marker, pattern in (
        ("internal_only", re.compile(r"\binternal-only\b", re.IGNORECASE)),
        ("partial_public", re.compile(r"\bpartial-public\b", re.IGNORECASE)),
        ("api_key", _SECRET_CONTENT),
    ):
        if pattern.search(text):
            signals.append(marker)
    return tuple(signals)


def build_export_artifacts(
    ir: SynapseIR,
    frame: CognitiveFrame,
    failures: FailureReport,
    plan: GuidedBuildPlan,
    blueprint: ProjectBlueprint,
) -> dict[str, bytes]:
    """Render only reviewed handoff artifacts; no filesystem writes occur."""
    ir.validate(strict=True)
    if frame.id != build_cognitive_frame(ir).id or plan.frame_id != frame.id:
        raise ExportError("IR, Cognitive Frame, Guided Build plan이 일치하지 않습니다.")
    if failures.to_record() != triage_frame(frame).to_record():
        raise ExportError("Cognitive Frame과 Failure Report가 일치하지 않습니다.")
    if blueprint.plan_id != plan.id:
        raise ExportError("Project Blueprint와 Guided Build plan이 일치하지 않습니다.")

    def document(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"

    return {
        "AGENTS.md": (
            b"# Synapse Portable Handoff\n\n"
            b"spec/ is authoritative. Review proposals before Canonical State commit.\n"
        ),
        "README.md": (
            f"# {blueprint.project_name}\n\n"
            "This workspace was exported through Synapse's reviewed portable handoff boundary.\n"
            f"Export level: `{blueprint.export_level}`.\n"
        ).encode(),
        "synapse/build/plan.json": document(plan.to_record()),
        "synapse/cognitive/frame.json": document(frame.to_record()),
        "synapse/decisions/README.md": (
            b"# Decisions\n\n"
            b"Decision ledger entries remain subject to owner and provenance review.\n"
        ),
        "synapse/failures/report.json": document(failures.to_record()),
        "synapse/ir/intake.json": document(ir.to_record()),
    }


def export_project(
    source_dir: str | Path,
    destination: str | Path,
    blueprint: ProjectBlueprint,
    *,
    artifacts: Mapping[str, bytes | str] | None = None,
    include_paths: Iterable[str] | None = None,
    approved: bool = False,
) -> PortableExportReport:
    """Write an approved, deterministic ZIP without changing source files."""
    root = Path(source_dir).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not root.is_dir():
        raise ExportError(f"source directory를 찾을 수 없습니다: {root}")
    if target == root or root in target.parents:
        raise ExportError("export destination은 source directory 밖이어야 합니다.")
    if target.exists():
        raise ExportError(f"이미 존재하는 export destination입니다: {target}")
    if blueprint.review_required and not approved:
        raise ExportError("owner approval 없이 review 상태 blueprint를 export할 수 없습니다.")

    selected: dict[str, tuple[bytes, str, tuple[str, ...], tuple[str, ...]]] = {}
    warnings: list[str] = []
    explicit = include_paths is not None
    requested = tuple(include_paths or ())
    candidates: list[Path] = []
    if explicit:
        for raw in requested:
            relative = _normalise_relative(raw)
            candidate = (root / Path(*PurePosixPath(relative).parts)).resolve()
            if root not in candidate.parents and candidate != root:
                raise ExportError(f"source directory 밖의 path입니다: {raw}")
            if candidate.is_symlink():
                raise ExportError(f"symlink source는 export할 수 없습니다: {relative}")
            if candidate.is_dir():
                candidates.extend(path for path in candidate.rglob("*") if path.is_file())
            elif candidate.is_file():
                candidates.append(candidate)
            else:
                raise ExportError(f"source path를 찾을 수 없습니다: {relative}")
    else:
        candidates = [path for path in root.rglob("*") if path.is_file()]

    for candidate in sorted(set(candidates), key=lambda path: path.as_posix().casefold()):
        if candidate.is_symlink():
            relative = candidate.relative_to(root).as_posix()
            if explicit:
                raise ExportError(f"symlink source는 export할 수 없습니다: {relative}")
            warnings.append(f"excluded:symlink:{relative}")
            continue
        relative = _normalise_relative(candidate.relative_to(root).as_posix())
        denied = _denied_reason(relative)
        if denied:
            if explicit:
                raise ExportError(f"private path는 export할 수 없습니다: {relative}")
            warnings.append(f"excluded:{denied}:{relative}")
            continue
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            raise ExportError(f"source file을 읽을 수 없습니다: {relative}") from exc
        signals = _content_signals(relative, data)
        if "api_key" in signals:
            if explicit:
                raise ExportError(f"secret-like content는 export할 수 없습니다: {relative}")
            warnings.append(f"excluded:secret_like_content:{relative}")
            continue
        selected[relative] = (data, "project_source", (), signals)

    for raw_path, raw_data in (artifacts or {}).items():
        relative = _normalise_relative(raw_path)
        if _denied_reason(relative):
            raise ExportError(f"generated artifact path는 허용되지 않습니다: {relative}")
        data = raw_data.encode("utf-8") if isinstance(raw_data, str) else bytes(raw_data)
        if relative in selected:
            if selected[relative][0] == data:
                continue
            if relative in {"README.md", "AGENTS.md"} and selected[relative][1] == "project_source":
                continue
            raise ExportError(f"source와 generated artifact가 충돌합니다: {relative}")
        selected[relative] = (data, "synapse_artifact", (), _content_signals(relative, data))

    blueprint_roles = {entry.path: entry for entry in blueprint.entries}
    export_entries = tuple(
        ExportEntry(
            path=path,
            size=len(data),
            sha256=_sha256(data),
            role=blueprint_roles.get(path).role if path in blueprint_roles else role,
            source_ids=blueprint_roles.get(path).source_ids if path in blueprint_roles else source_ids,
            signals=signals,
        )
        for path, (data, role, source_ids, signals) in sorted(selected.items())
    )
    privacy_signals = tuple(sorted({signal for entry in export_entries for signal in entry.signals}))
    if (warnings or privacy_signals) and not approved:
        raise ExportError("privacy review가 필요한 export는 owner approval이 필요합니다.")

    manifest_payload = {
        "format": "synapse-portable-bundle",
        "version": 1,
        "blueprint_id": blueprint.id,
        "export_level": blueprint.export_level,
        "canonical_revision": 0,
        "source_ids": sorted({source_id for entry in export_entries for source_id in entry.source_ids}),
        "created_at": f"deterministic:{blueprint.id}",
        "entries": [entry.to_record() for entry in export_entries],
        "privacy_policy": {
            "allowlist": True,
            "excluded_patterns": sorted((*_DENIED_SEGMENTS, "secret-like filenames/content")),
            "signals": list(privacy_signals),
            "approval_required": bool(blueprint.review_required or warnings or privacy_signals),
        },
    }
    manifest_digest = _sha256(_canonical_json(manifest_payload))
    manifest = dict(manifest_payload)
    manifest["sha256"] = manifest_digest
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    selected["synapse.bundle.json"] = (manifest_bytes, "manifest", (), ())

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(selected):
                info = zipfile.ZipInfo(path)
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, selected[path][0])
        os.replace(temporary, target)
    except (OSError, zipfile.BadZipFile) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExportError(f"portable ZIP을 저장할 수 없습니다: {target}") from exc

    return PortableExportReport(
        archive=str(target),
        archive_sha256=_sha256(target.read_bytes()),
        manifest_sha256=manifest_digest,
        export_level=blueprint.export_level,
        entries=export_entries,
        warnings=tuple(sorted(warnings)),
    )


__all__ = [
    "ExportEntry",
    "ExportError",
    "PortableExportReport",
    "build_export_artifacts",
    "export_project",
]
