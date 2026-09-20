"""Deterministic intake adapters from reviewed bundle reports to Synapse IR."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from synapse.core.bundle import BundleReport, inspect_bundle, load_bundle_report
from synapse.core.contracts import LifecycleStatus, Provenance
from synapse.core.ir import IRError, IRNode, IRRelation, SynapseIR, build_ir


def _entry_id(path: str) -> str:
    return f"bundle-entry:{hashlib.sha256(path.encode('utf-8')).hexdigest()}"


def _observation_provenance(report: BundleReport, *, method: str, locator: str) -> Provenance:
    """Use the archive fingerprint as the stable observation epoch."""
    # The archive hash is the stable identity.  Do not persist an absolute
    # local path (which can contain a user's account name) in an IR record.
    safe_locator = Path(report.archive).name if locator == report.archive else locator
    return Provenance(
        source_id=report.source_id,
        method=method,
        locator=safe_locator,
        captured_at=f"archive:{report.archive_sha256}",
    )


def bundle_report_to_ir(report: BundleReport, *, owner: str) -> SynapseIR:
    """Convert archive observations to PROPOSED IR without semantic guessing."""
    if not owner.strip():
        raise IRError("bundle intake에는 owner가 필요합니다.")
    if not report.safe:
        raise IRError("안전 검사를 통과하지 못한 bundle은 IR로 intake할 수 없습니다.")
    root_id = f"bundle:{report.archive_sha256}"
    root_provenance = (
        _observation_provenance(
            report,
            method="bundle_inspection",
            locator=report.archive,
        ),
    )
    nodes = [
        IRNode(
            id=root_id,
            kind="bundle",
            label=Path(report.archive).name,
            owner=owner,
            status=LifecycleStatus.PROPOSED,
            provenance=root_provenance,
            attributes={
                "archive_sha256": report.archive_sha256,
                "entry_count": report.entry_count,
                "total_uncompressed_size": report.total_uncompressed_size,
                "content_summary": dict(report.content_summary),
            },
        )
    ]
    relations: list[IRRelation] = []
    for entry in report.entries:
        entry_id = _entry_id(entry.path)
        provenance = (
            _observation_provenance(
                report,
                method="bundle_entry_observation",
                locator=entry.path,
            ),
        )
        nodes.append(
            IRNode(
                id=entry_id,
                kind=f"bundle_{entry.kind}",
                label=entry.title or entry.path,
                owner=owner,
                status=LifecycleStatus.PROPOSED,
                provenance=provenance,
                attributes={
                    "path": entry.path,
                    "size": entry.size,
                    "sha256": entry.sha256,
                    "headings": entry.headings,
                    "signals": entry.signals,
                },
            )
        )
        relations.append(
            IRRelation(
                id=f"contains:{root_id}:{entry_id}",
                source_id=root_id,
                target_id=entry_id,
                relation="contains",
                owner=owner,
                status=LifecycleStatus.PROPOSED,
                provenance=root_provenance,
            )
        )
    return build_ir(
        nodes,
        relations,
        metadata={"intake": "bundle_report", "source_id": report.source_id},
        strict=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a reviewed ZIP observation into PROPOSED Synapse IR")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--owner", default="human")
    parser.add_argument("--report", type=Path, help="기존 report를 원본 ZIP hash와 함께 재검증")
    args = parser.parse_args(argv)
    report = load_bundle_report(args.report, archive=args.archive) if args.report else inspect_bundle(args.archive)
    ir = bundle_report_to_ir(report, owner=args.owner)
    print(json.dumps(ir.to_record(), ensure_ascii=False, indent=2))
    return 0


__all__ = ["bundle_report_to_ir", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
