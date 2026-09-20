"""Read-only inspection of portable Synapse ZIP bundles.

This module is intentionally an intake/observation boundary, not an import
commit path. It reads a ZIP without extracting or executing anything, checks
for traversal/symlink/size hazards, fingerprints each entry, and reports the
project shape so a later phase can turn reviewed observations into proposals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 20_000
DEFAULT_MAX_ENTRY_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
MANIFEST_NAME = "synapse.bundle.json"


class BundleError(ValueError):
    """Raised when a ZIP cannot be safely inspected as a portable bundle."""


@dataclass(frozen=True, slots=True)
class BundleEntry:
    path: str
    size: int
    sha256: str
    kind: str
    title: str | None = None
    headings: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BundleReport:
    archive: str
    archive_sha256: str
    entry_count: int
    total_uncompressed_size: int
    roots: tuple[str, ...]
    key_files: tuple[str, ...]
    entries: tuple[BundleEntry, ...]
    manifest: Mapping[str, Any] | None
    content_summary: Mapping[str, Any]
    warnings: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.warnings

    @property
    def source_id(self) -> str:
        """Stable provenance root for this exact archive content."""
        return f"source:bundle:{self.archive_sha256}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_entry_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise BundleError("ZIP entry 이름이 비어 있거나 NUL을 포함합니다.")
    normalised = name.replace("\\", "/")
    path = PurePosixPath(normalised)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BundleError(f"ZIP path traversal 또는 절대 경로가 감지되었습니다: {name!r}")
    result = str(path)
    return result.rstrip("/") if result != "." else ""


def _entry_kind(path: str) -> str:
    lower = path.lower()
    if lower == MANIFEST_NAME:
        return "manifest"
    if lower == "agents.md" or lower.startswith("spec/"):
        return "authority"
    if lower.startswith(("docs/", "doc/")):
        return "documentation"
    if lower.startswith("tests/"):
        return "tests"
    if lower.startswith(("synapse/", "domain_packs/")):
        return "source"
    if lower.endswith((".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".md", ".toml")):
        return "text"
    return "asset"


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix file type bits are stored in the high word when an archive was
    # created on Unix. A symlink is never extracted or followed by this tool.
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


_MARKERS = (
    ("internal_only", re.compile(r"\binternal-only\b", re.IGNORECASE)),
    ("partial_public", re.compile(r"\bpartial-public\b", re.IGNORECASE)),
    ("api_key", re.compile(r"\bapi\s*key\b", re.IGNORECASE)),
    ("checkpoint", re.compile(r"\bcheckpoint\b", re.IGNORECASE)),
    ("project_connection", re.compile(r"\bproject\s+connection\b", re.IGNORECASE)),
    ("provenance", re.compile(r"\bprovenance\b", re.IGNORECASE)),
    ("canonical_state", re.compile(r"\bcanonical\s+state\b", re.IGNORECASE)),
)


def _text_analysis(path: str, data: bytes) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    if not path.lower().endswith((".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml")):
        return None, (), ()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, (), ()
    headings = tuple(
        match.group(1).strip()
        for match in re.finditer(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", text, re.MULTILINE)
        if match.group(1).strip()
    )
    title = headings[0] if headings else None
    signals = tuple(name for name, marker in _MARKERS if marker.search(text))
    return title, headings[:32], signals


def _report_dict(report: BundleReport) -> dict[str, Any]:
    return asdict(report)


def save_bundle_report(report: BundleReport, path: str | Path) -> Path:
    """Atomically persist an intake report without importing its contents."""
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "synapse-bundle-report",
        "version": 1,
        "source_id": report.source_id,
        "report": _report_dict(report),
    }
    document = {"payload": payload, "sha256": hashlib.sha256(_canonical_json(payload)).hexdigest()}
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BundleError(f"bundle report를 저장할 수 없습니다: {destination}") from exc
    return destination


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_bundle_report(path: str | Path, *, archive: str | Path | None = None) -> BundleReport:
    """Reload a report and optionally verify it against the current ZIP."""
    source = Path(path).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"bundle report를 읽을 수 없습니다: {source}") from exc
    if not isinstance(document, Mapping):
        raise BundleError("bundle report 최상위 문서는 객체여야 합니다.")
    payload = document.get("payload")
    digest = document.get("sha256")
    if not isinstance(payload, Mapping) or not isinstance(digest, str):
        raise BundleError("bundle report integrity envelope가 없습니다.")
    if digest != hashlib.sha256(_canonical_json(payload)).hexdigest():
        raise BundleError("bundle report checksum이 일치하지 않습니다.")
    if payload.get("format") != "synapse-bundle-report" or payload.get("version") != 1:
        raise BundleError("지원하지 않는 bundle report format/version입니다.")
    raw = payload.get("report")
    if not isinstance(raw, Mapping) or payload.get("source_id") != f"source:bundle:{raw.get('archive_sha256', '')}":
        raise BundleError("bundle report source identity가 일치하지 않습니다.")
    try:
        entries = tuple(
            BundleEntry(
                path=str(entry["path"]),
                size=int(entry["size"]),
                sha256=str(entry["sha256"]),
                kind=str(entry["kind"]),
                title=entry.get("title"),
                headings=tuple(entry.get("headings", ())),
                signals=tuple(entry.get("signals", ())),
            )
            for entry in raw["entries"]
        )
        report = BundleReport(
            archive=str(raw["archive"]),
            archive_sha256=str(raw["archive_sha256"]),
            entry_count=int(raw["entry_count"]),
            total_uncompressed_size=int(raw["total_uncompressed_size"]),
            roots=tuple(raw.get("roots", ())),
            key_files=tuple(raw.get("key_files", ())),
            entries=entries,
            manifest=raw.get("manifest"),
            content_summary=raw.get("content_summary", {}),
            warnings=tuple(raw.get("warnings", ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleError("bundle report 구조가 잘못되었습니다.") from exc
    if report.source_id != payload["source_id"]:
        raise BundleError("bundle report source_id가 archive hash와 다릅니다.")
    if archive is not None:
        current = inspect_bundle(archive)
        # ``archive`` is a locator, not part of the observed ZIP content. A
        # caller may move or copy an unchanged archive before reusing its
        # report. Compare every observed field while rebinding that locator to
        # the current archive, then return the current observation so later
        # proposal checks stay bound to the path the caller supplied.
        expected = replace(report, archive=current.archive)
        if current != expected:
            raise BundleError(
                "저장된 bundle report가 현재 ZIP의 관찰 결과와 다릅니다."
            )
        report = current
    return report


def inspect_bundle(
    archive: str | Path,
    *,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> BundleReport:
    """Inspect a ZIP file without extracting or executing its contents."""
    path = Path(archive).expanduser().resolve()
    if not path.is_file():
        raise BundleError(f"ZIP 파일을 찾을 수 없습니다: {path}")
    archive_size = path.stat().st_size
    if archive_size > max_archive_bytes:
        raise BundleError(f"ZIP 파일이 허용 크기를 초과합니다: {archive_size}")

    entries: list[BundleEntry] = []
    names_seen: set[str] = set()
    total_size = 0
    manifest: Mapping[str, Any] | None = None
    marker_counts: dict[str, int] = {}
    try:
        with zipfile.ZipFile(path) as source:
            infos = source.infolist()
            if len(infos) > max_entries:
                raise BundleError(f"ZIP entry 수가 허용 한도를 초과합니다: {len(infos)}")
            for info in infos:
                name = _normalise_entry_name(info.filename)
                if not name:
                    continue
                collision_key = name.casefold()
                if collision_key in names_seen:
                    raise BundleError(f"대소문자 무시 시 중복되는 ZIP entry입니다: {name}")
                names_seen.add(collision_key)
                if _is_symlink(info):
                    raise BundleError(f"symlink ZIP entry는 허용하지 않습니다: {name}")
                if info.file_size > max_entry_bytes:
                    raise BundleError(f"ZIP entry가 허용 크기를 초과합니다: {name}")
                total_size += info.file_size
                if total_size > max_total_bytes:
                    raise BundleError("ZIP의 압축 해제 총량이 허용 한도를 초과합니다.")
                if info.is_dir():
                    continue
                with source.open(info, "r") as handle:
                    data = handle.read(max_entry_bytes + 1)
                if len(data) != info.file_size:
                    raise BundleError(f"ZIP entry 크기를 검증할 수 없습니다: {name}")
                if name == MANIFEST_NAME:
                    try:
                        loaded = json.loads(data.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise BundleError("synapse.bundle.json을 읽을 수 없습니다.") from exc
                    if not isinstance(loaded, Mapping):
                        raise BundleError("synapse.bundle.json은 JSON 객체여야 합니다.")
                    manifest = dict(loaded)
                title, headings, signals = _text_analysis(name, data)
                for signal in signals:
                    marker_counts[signal] = marker_counts.get(signal, 0) + 1
                entries.append(
                    BundleEntry(
                        path=name,
                        size=info.file_size,
                        sha256=hashlib.sha256(data).hexdigest(),
                        kind=_entry_kind(name),
                        title=title,
                        headings=headings,
                        signals=signals,
                    )
                )
    except BundleError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BundleError(f"ZIP 파일을 안전하게 읽을 수 없습니다: {path}") from exc

    paths = tuple(entry.path for entry in entries)
    roots = tuple(sorted({entry.path.split("/", 1)[0] for entry in entries}))
    key_files = tuple(
        path_name
        for path_name in paths
        if path_name.casefold() in {"agents.md", "readme.md", "pyproject.toml", "package.json"}
        or path_name.casefold().startswith("spec/")
        or path_name.casefold() == ".openai/hosting.json"
    )
    return BundleReport(
        archive=str(path),
        archive_sha256=_sha256_file(path),
        entry_count=len(entries),
        total_uncompressed_size=total_size,
        roots=roots,
        key_files=key_files,
        entries=tuple(sorted(entries, key=lambda entry: entry.path)),
        manifest=manifest,
        content_summary={
            "text_files": sum(1 for entry in entries if entry.title or entry.headings or entry.signals),
            "markdown_files": sum(1 for entry in entries if entry.path.lower().endswith(".md")),
            "marker_file_counts": dict(sorted(marker_counts.items())),
            "titles": {
                entry.path: entry.title
                for entry in sorted(entries, key=lambda entry: entry.path)
                if entry.title
            },
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect a Synapse ZIP bundle safely")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--report", type=Path, help="분석 report를 JSON으로 저장할 경로")
    args = parser.parse_args(argv)
    try:
        report = inspect_bundle(args.archive)
    except BundleError as exc:
        parser.error(str(exc))
    if args.report:
        save_bundle_report(report, args.report)
    print(json.dumps(_report_dict(report), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "BundleEntry",
    "BundleError",
    "BundleReport",
    "inspect_bundle",
    "load_bundle_report",
    "save_bundle_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
