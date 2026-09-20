"""Verified, no-execution staging for portable Synapse bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from synapse.core.bundle import BundleError, BundleReport, inspect_bundle, load_bundle_report
from synapse.core.cognitive import CognitiveFrame, build_cognitive_frame
from synapse.core.failure import FailureReport, triage_frame
from synapse.core.intake import bundle_report_to_ir
from synapse.core.ir import SynapseIR
from synapse.core.premise import PremisePartition, partition_premises


class StageError(ValueError):
    """Raised when a portable bundle cannot be safely staged."""


@dataclass(frozen=True, slots=True)
class StagedBundle:
    archive: str
    archive_sha256: str
    staging_dir: str
    source_id: str
    staged_paths: tuple[str, ...]
    report: BundleReport
    ir: SynapseIR
    premise: PremisePartition
    frame: CognitiveFrame
    failures: FailureReport

    def __post_init__(self) -> None:
        object.__setattr__(self, "staged_paths", tuple(sorted(self.staged_paths)))

    def to_record(self) -> dict[str, object]:
        return {
            "archive": self.archive,
            "archive_sha256": self.archive_sha256,
            "staging_dir": self.staging_dir,
            "source_id": self.source_id,
            "staged_paths": list(self.staged_paths),
            "ir_node_count": len(self.ir.nodes),
            "ir_relation_count": len(self.ir.relations),
            "premise": self.premise.to_record(),
            "frame_id": self.frame.id,
            "failure_report": self.failures.to_record(),
            "canonical_mutation": False,
        }


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(raw: str) -> str:
    normalised = raw.replace("\\", "/")
    path = PurePosixPath(normalised)
    if not normalised or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StageError(f"안전하지 않은 staged path입니다: {raw!r}")
    return str(path)


def _verify_manifest(report: BundleReport, *, approved: bool) -> None:
    manifest = report.manifest
    if not manifest:
        return
    if manifest.get("format") != "synapse-portable-bundle" or manifest.get("version") != 1:
        raise StageError("지원하지 않는 portable bundle manifest입니다.")
    stored_digest = manifest.get("sha256")
    if not isinstance(stored_digest, str):
        raise StageError("portable bundle manifest checksum이 없습니다.")
    payload = dict(manifest)
    payload.pop("sha256", None)
    if _sha256(_canonical_json(payload)) != stored_digest:
        raise StageError("portable bundle manifest checksum이 일치하지 않습니다.")
    privacy = manifest.get("privacy_policy")
    if isinstance(privacy, dict) and privacy.get("approval_required") and not approved:
        raise StageError("privacy approval 없이 portable bundle을 stage할 수 없습니다.")
    report_entries = {entry.path: entry for entry in report.entries}
    raw_entries = manifest.get("entries", ())
    if not isinstance(raw_entries, list):
        raise StageError("portable bundle manifest entries가 잘못되었습니다.")
    for raw in raw_entries:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise StageError("portable bundle manifest entry가 잘못되었습니다.")
        path = raw["path"]
        observed = report_entries.get(path)
        if observed is None or observed.sha256 != raw.get("sha256") or observed.size != raw.get("size"):
            raise StageError(f"portable bundle manifest와 archive entry가 다릅니다: {path}")


def stage_bundle(
    archive: str | Path,
    staging_dir: str | Path,
    *,
    report: BundleReport | str | Path | None = None,
    owner: str = "human",
    approved: bool = False,
) -> StagedBundle:
    """Verify and copy a bundle into a new staging directory without executing it."""
    archive_path = Path(archive).expanduser().resolve()
    target = Path(staging_dir).expanduser().resolve()
    try:
        if report is None:
            bundle_report = inspect_bundle(archive_path)
        elif isinstance(report, BundleReport):
            bundle_report = report
            if bundle_report.archive_sha256 != inspect_bundle(archive_path).archive_sha256:
                raise StageError("archive hash와 supplied report가 다릅니다.")
        else:
            bundle_report = load_bundle_report(report, archive=archive_path)
    except BundleError as exc:
        raise StageError(str(exc)) from exc
    if not bundle_report.safe:
        raise StageError("unsafe bundle은 stage할 수 없습니다.")
    _verify_manifest(bundle_report, approved=approved)
    if target.exists():
        raise StageError(f"staging target already exists (이미 존재합니다): {target}")
    if not owner.strip():
        raise StageError("staging owner가 비어 있습니다.")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.staging"
    staged_paths: list[str] = []
    try:
        temporary.mkdir()
        with zipfile.ZipFile(archive_path) as source:
            for entry in bundle_report.entries:
                relative = _safe_relative(entry.path)
                info = source.getinfo(entry.path)
                with source.open(info, "r") as handle:
                    data = handle.read()
                if len(data) != entry.size or _sha256(data) != entry.sha256:
                    raise StageError(f"archive entry hash가 report와 다릅니다: {relative}")
                destination = temporary.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                staged_paths.append(relative)
        os.replace(temporary, target)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise StageError(f"bundle staging에 실패했습니다: {archive_path}") from exc
    except StageError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    try:
        ir = bundle_report_to_ir(bundle_report, owner=owner)
        premise = partition_premises(ir)
        frame = build_cognitive_frame(ir)
        failures = triage_frame(frame)
    except (BundleError, ValueError) as exc:
        # The target is ours and has just been created; leave it as an evidence
        # snapshot rather than attempting a broad cleanup of user data.
        raise StageError(f"staged bundle IR을 만들 수 없습니다: {exc}") from exc
    return StagedBundle(
        archive=str(archive_path),
        archive_sha256=bundle_report.archive_sha256,
        staging_dir=str(target),
        source_id=bundle_report.source_id,
        staged_paths=tuple(staged_paths),
        report=bundle_report,
        ir=ir,
        premise=premise,
        frame=frame,
        failures=failures,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage a Synapse bundle without executing it")
    parser.add_argument("archive", type=Path)
    parser.add_argument("staging_dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--owner", default="human")
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = stage_bundle(
            args.archive,
            args.staging_dir,
            report=args.report,
            owner=args.owner,
            approved=args.approved,
        )
    except StageError as exc:
        parser.error(str(exc))
    print(json.dumps(result.to_record(), ensure_ascii=False, indent=2))
    return 0


__all__ = ["StageError", "StagedBundle", "main", "stage_bundle"]


if __name__ == "__main__":
    raise SystemExit(main())
