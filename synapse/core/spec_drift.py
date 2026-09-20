"""Read-only manifest checks for projection drift observation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".yaml", ".yml", ".json"})
_ANCHOR_PATTERN = re.compile(r"^#{1,2}[ \t]+(.+?)[ \t]*$")


class SpecDriftError(ValueError):
    """Raised when the drift manifest itself cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class DriftFinding:
    """One deterministic finding associated with a manifest entry."""

    projection: str
    target: str

    def __post_init__(self) -> None:
        if not isinstance(self.projection, str) or not self.projection.strip():
            raise SpecDriftError("drift finding projection은 비어 있을 수 없습니다.")
        if not isinstance(self.target, str) or not self.target.strip():
            raise SpecDriftError("drift finding target은 비어 있을 수 없습니다.")

    def as_dict(self) -> dict[str, str]:
        return {"projection": self.projection, "target": self.target}


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Immutable observation result; ``passed`` means the checker ran cleanly."""

    ok: tuple[str, ...] = ()
    missing_file: tuple[DriftFinding, ...] = ()
    broken_anchor: tuple[DriftFinding, ...] = ()
    missing_marker: tuple[DriftFinding, ...] = ()
    invalid_path: tuple[DriftFinding, ...] = ()
    unsupported_format: tuple[DriftFinding, ...] = ()
    unreadable_file: tuple[DriftFinding, ...] = ()
    skipped: tuple[str, ...] = ()
    checked_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("missing_file", "broken_anchor", "missing_marker", "invalid_path", "unsupported_format", "unreadable_file"):
            raw_findings = getattr(self, field_name)
            if isinstance(raw_findings, (str, bytes)):
                raise SpecDriftError(f"drift report의 {field_name}은 finding sequence여야 합니다.")
            findings = tuple(raw_findings)
            if not all(isinstance(finding, DriftFinding) for finding in findings):
                raise SpecDriftError(f"drift report의 {field_name}에 잘못된 finding이 있습니다.")
            object.__setattr__(self, field_name, _sorted_findings(list(findings)))
        for field_name in ("ok", "skipped", "checked_files"):
            raw_values = getattr(self, field_name)
            if isinstance(raw_values, (str, bytes)):
                raise SpecDriftError(f"drift report의 {field_name}은 string sequence여야 합니다.")
            values = tuple(raw_values)
            if not all(isinstance(value, str) and value.strip() for value in values):
                raise SpecDriftError(f"drift report의 {field_name}에 잘못된 값이 있습니다.")
            object.__setattr__(self, field_name, tuple(sorted(values)))

    @property
    def passed(self) -> bool:
        """Return whether no drift or manifest-path error was observed."""

        return not any(
            (
                self.missing_file,
                self.broken_anchor,
                self.missing_marker,
                self.invalid_path,
                self.unsupported_format,
                self.unreadable_file,
            )
        )

    @property
    def issue_count(self) -> int:
        return sum(
            len(findings)
            for findings in (
                self.missing_file,
                self.broken_anchor,
                self.missing_marker,
                self.invalid_path,
                self.unsupported_format,
                self.unreadable_file,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "issue_count": self.issue_count,
            "ok": list(self.ok),
            "missing_file": [finding.as_dict() for finding in self.missing_file],
            "broken_anchor": [finding.as_dict() for finding in self.broken_anchor],
            "missing_marker": [finding.as_dict() for finding in self.missing_marker],
            "invalid_path": [finding.as_dict() for finding in self.invalid_path],
            "unsupported_format": [
                finding.as_dict() for finding in self.unsupported_format
            ],
            "unreadable_file": [finding.as_dict() for finding in self.unreadable_file],
            "skipped": list(self.skipped),
            "checked_files": list(self.checked_files),
        }


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    path: str
    covers_spec_ids: tuple[str, ...]
    required_markers: tuple[str, ...]
    existence_only: bool


def check_projection_drift(
    manifest_path: str | Path,
    *,
    root: str | Path | None = None,
) -> DriftReport:
    """Inspect declared projections without writing any repository content.

    Relative projection paths are resolved below the repository root. Bare YAML
    anchor paths are resolved below ``root/spec``; paths beginning with
    ``spec/`` and Markdown anchor paths are resolved below ``root``.
    """

    manifest = Path(manifest_path).resolve()
    repository_root = (
        Path(root).resolve() if root is not None else manifest.parent.parent.resolve()
    )
    document = _load_manifest(manifest)
    entries = _manifest_entries(document)

    ok: list[str] = []
    skipped: list[str] = []
    checked_files: set[str] = set()
    missing_file: list[DriftFinding] = []
    broken_anchor: list[DriftFinding] = []
    missing_marker: list[DriftFinding] = []
    invalid_path: list[DriftFinding] = []
    unsupported_format: list[DriftFinding] = []
    unreadable_file: list[DriftFinding] = []

    for entry in entries:
        label = _normalise_label(entry.path)
        target = _safe_resolve(repository_root, entry.path)
        if target is None:
            invalid_path.append(DriftFinding(label, entry.path))
            continue
        if not target.exists():
            missing_file.append(DriftFinding(label, entry.path))
            continue

        content: str | None = None
        if not entry.existence_only and entry.required_markers:
            content, files, read_error = _read_projection_text(target)
            checked_files.update(_relative_name(repository_root, path) for path in files)
            if read_error is not None:
                if read_error == "unsupported_format":
                    unsupported_format.append(DriftFinding(label, entry.path))
                else:
                    unreadable_file.append(DriftFinding(label, entry.path))

        for anchor in entry.covers_spec_ids:
            if not _anchor_exists(repository_root, anchor):
                broken_anchor.append(DriftFinding(label, anchor))

        if entry.required_markers and content is not None:
            for marker in entry.required_markers:
                if marker not in content:
                    missing_marker.append(DriftFinding(label, marker))

        has_content_checks = bool(entry.covers_spec_ids or entry.required_markers)
        if not has_content_checks:
            skipped.append(label)
        if not any(
            item.projection == label
            for finding_group in (
                missing_file,
                broken_anchor,
                missing_marker,
                invalid_path,
                unsupported_format,
                unreadable_file,
            )
            for item in finding_group
        ):
            ok.append(label)

        if target.is_file():
            checked_files.add(_relative_name(repository_root, target))

    return DriftReport(
        ok=tuple(sorted(ok)),
        missing_file=_sorted_findings(missing_file),
        broken_anchor=_sorted_findings(broken_anchor),
        missing_marker=_sorted_findings(missing_marker),
        invalid_path=_sorted_findings(invalid_path),
        unsupported_format=_sorted_findings(unsupported_format),
        unreadable_file=_sorted_findings(unreadable_file),
        skipped=tuple(sorted(skipped)),
        checked_files=tuple(sorted(checked_files)),
    )


def _load_manifest(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise SpecDriftError(f"projection manifest를 찾을 수 없습니다: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SpecDriftError(f"projection manifest를 읽을 수 없습니다: {path}") from exc
    if not isinstance(document, Mapping):
        raise SpecDriftError("projection manifest는 mapping이어야 합니다.")
    return document


def _manifest_entries(document: Mapping[str, Any]) -> tuple[_ManifestEntry, ...]:
    entries: list[_ManifestEntry] = []
    seen_paths: set[str] = set()
    for section in ("projections", "handoff_documents"):
        raw_entries = document.get(section, [])
        if raw_entries is None:
            continue
        if not isinstance(raw_entries, list):
            raise SpecDriftError(f"manifest의 {section}은 list여야 합니다.")
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping):
                raise SpecDriftError(f"manifest의 {section} 항목은 mapping이어야 합니다.")
            raw_path = raw_entry.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise SpecDriftError(f"manifest의 {section} 항목 path가 필요합니다.")
            label = _normalise_label(raw_path)
            if label in seen_paths:
                raise SpecDriftError(f"manifest projection path가 중복됩니다: {label}")
            seen_paths.add(label)
            covers = _string_list(raw_entry, "covers_spec_ids")
            markers = _string_list(raw_entry, "required_markers")
            mode = raw_entry.get("drift_check")
            if mode is not None and mode != "existence_only":
                raise SpecDriftError(f"지원하지 않는 drift_check입니다: {mode}")
            if mode == "existence_only" and (covers or markers):
                raise SpecDriftError(
                    "existence_only 항목에는 covers_spec_ids/required_markers를 함께 둘 수 없습니다."
                )
            entries.append(
                _ManifestEntry(
                    path=raw_path,
                    covers_spec_ids=covers,
                    required_markers=markers,
                    existence_only=mode == "existence_only",
                )
            )
    return tuple(entries)


def _string_list(entry: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw_values = entry.get(key, [])
    if raw_values is None:
        return ()
    if not isinstance(raw_values, list):
        raise SpecDriftError(f"manifest의 {key}은 list여야 합니다.")
    values: list[str] = []
    for value in raw_values:
        if not isinstance(value, str) or not value.strip():
            raise SpecDriftError(f"manifest의 {key}에는 비어 있지 않은 문자열만 허용됩니다.")
        if value not in values:
            values.append(value)
    return tuple(values)


def _normalise_label(path: str) -> str:
    return path.replace("\\", "/")


def _safe_resolve(root: Path, raw_path: str) -> Path | None:
    relative = Path(raw_path)
    if relative.is_absolute():
        return None
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _relative_name(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _read_projection_text(path: Path) -> tuple[str | None, tuple[Path, ...], str | None]:
    if path.is_file():
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            return None, (path,), "unsupported_format"
        try:
            return path.read_text(encoding="utf-8"), (path,), None
        except (OSError, UnicodeError):
            return None, (path,), "unreadable_file"

    files = tuple(
        sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in _TEXT_SUFFIXES
        )
    )
    contents: list[str] = []
    for file_path in files:
        try:
            contents.append(file_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None, files, "unreadable_file"
    return "\n".join(contents), files, None


def _anchor_exists(root: Path, anchor: str) -> bool:
    source, separator, name = anchor.partition("#")
    if not separator or not source.strip() or not name.strip() or "#" in name:
        return False
    source_path = Path(source)
    if source_path.is_absolute() or any(part == ".." for part in source_path.parts):
        return False
    if source.replace("\\", "/").startswith("spec/"):
        path = _safe_resolve(root, source)
    elif source_path.suffix.lower() in {".yaml", ".yml"}:
        path = _safe_resolve(root / "spec", source)
    else:
        path = _safe_resolve(root, source)
    if path is None or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            return False
        return isinstance(document, Mapping) and name in document
    if path.suffix.lower() in {".md", ".markdown"}:
        return any(_markdown_heading(line) == name.strip() for line in text.splitlines())
    return False


def _markdown_heading(line: str) -> str | None:
    match = _ANCHOR_PATTERN.match(line.strip())
    if match is None:
        return None
    heading = match.group(1).strip()
    return re.sub(r"[ \t]+#+[ \t]*$", "", heading).strip()


def _sorted_findings(findings: list[DriftFinding]) -> tuple[DriftFinding, ...]:
    return tuple(sorted(findings, key=lambda finding: (finding.projection, finding.target)))


__all__ = ["DriftFinding", "DriftReport", "SpecDriftError", "check_projection_drift"]
