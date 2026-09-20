"""Read-only integrity inspection for one Phase 50 structural report.

The Phase 50 writer owns report materialization.  This module only reads one
caller-selected ``structural.export.report.v1`` file, rebuilds the existing
immutable contracts, and checks their stored identities.  It never re-runs an
export review, invokes a sensor, or writes a repaired or derived file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_adapter import (
    StructuralImport,
    StructuralImportReceipt,
)
from synapse.core.structural_export_report import (
    STRUCTURAL_EXPORT_REPORT_SCHEMA,
    StructuralExportReportArtifact,
)
from synapse.core.structural_export_review import (
    STRUCTURAL_EXPORT_REVIEW_SCHEMA,
    STRUCTURAL_EXPORT_REVIEW_STAGES,
    StructuralExportReviewResult,
)
from synapse.core.structural_observation import parse_structural_observation
from synapse.core.structural_policy import (
    StructuralFinding,
    StructuralPolicyReport,
    StructuralPolicyRule,
)
from synapse.core.structural_policy_gate import StructuralPolicyGateResult

STRUCTURAL_EXPORT_REPORT_INSPECTION_SCHEMA = "structural.export.report.inspection.v1"
STRUCTURAL_EXPORT_REPORT_INSPECTION_ERROR_EXIT_CODE = 6
MAX_STRUCTURAL_EXPORT_REPORT_BYTES = 16 * 1024 * 1024
_MAX_TEXT = 4_096
_ERROR_KINDS = frozenset(
    {
        "INVALID_INPUT",
        "FILE_READ_FAILED",
        "INVALID_UTF8",
        "REPORT_TOO_LARGE",
        "DUPLICATE_JSON_KEY",
        "UNKNOWN_FIELD",
        "CONTRACT_REJECTED",
        "INTEGRITY_FAILED",
    }
)


class StructuralExportReportInspectionError(ValueError):
    """Raised when a supplied report cannot be verified safely."""

    def __init__(self, message: str, *, error_kind: str = "INVALID_INPUT") -> None:
        normalized_kind = str(error_kind).strip().upper()
        if normalized_kind not in _ERROR_KINDS:
            normalized_kind = "INVALID_INPUT"
        self.error_kind = normalized_kind
        super().__init__(message)


class _DuplicateJsonKeyError(ValueError):
    """Internal marker for duplicate keys rejected during JSON decoding."""


def _text(value: Any, label: str, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuralExportReportInspectionError(
            f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.",
            error_kind="INVALID_INPUT",
        )
    result = value.strip()
    if "\x00" in result:
        raise StructuralExportReportInspectionError(
            f"{label}에 허용되지 않은 NUL 문자가 있습니다.",
            error_kind="INVALID_INPUT",
        )
    if len(result) > limit:
        raise StructuralExportReportInspectionError(
            f"{label}이(가) 너무 깁니다.",
            error_kind="INVALID_INPUT",
        )
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructuralExportReportInspectionError(
            f"{label}는 JSON 객체여야 합니다.",
            error_kind="CONTRACT_REJECTED",
        )
    if any(not isinstance(key, str) for key in value):
        raise StructuralExportReportInspectionError(
            f"{label}의 객체 키는 문자열이어야 합니다.",
            error_kind="CONTRACT_REJECTED",
        )
    return value


def _strict_mapping(
    value: Any,
    label: str,
    *,
    keys: frozenset[str],
) -> Mapping[str, Any]:
    record = _mapping(value, label)
    unknown = sorted(set(record) - keys)
    if unknown:
        raise StructuralExportReportInspectionError(
            f"{label}에 허용되지 않은 field가 있습니다: {', '.join(unknown)}",
            error_kind="UNKNOWN_FIELD",
        )
    missing = sorted(keys - set(record))
    if missing:
        raise StructuralExportReportInspectionError(
            f"{label}에 필요한 field가 없습니다: {', '.join(missing)}",
            error_kind="CONTRACT_REJECTED",
        )
    return record


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise StructuralExportReportInspectionError(
            f"{label}는 JSON 배열이어야 합니다.",
            error_kind="CONTRACT_REJECTED",
        )
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise StructuralExportReportInspectionError(
            f"{label}은(는) boolean이어야 합니다.",
            error_kind="CONTRACT_REJECTED",
        )
    return value


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise _DuplicateJsonKeyError(key)
        record[key] = value
    return record


def _parse_rule(value: Any, index: int) -> StructuralPolicyRule:
    record = _strict_mapping(
        value,
        f"review.policy_report.rules[{index}]",
        keys=frozenset(
            {
                "id",
                "description",
                "relations",
                "source_layer",
                "target_layer",
                "severity",
                "schema_version",
            }
        ),
    )
    try:
        return StructuralPolicyRule(**dict(record))
    except (TypeError, ValueError) as exc:
        raise StructuralExportReportInspectionError(
            f"policy rule을 복원하지 못했습니다: {exc}",
            error_kind="CONTRACT_REJECTED",
        ) from exc


def _parse_finding(value: Any, index: int) -> StructuralFinding:
    record = _strict_mapping(
        value,
        f"review.policy_report.findings[{index}]",
        keys=frozenset(
            {
                "id",
                "finding_hash",
                "rule_id",
                "status",
                "severity",
                "observation_id",
                "source_id",
                "target_id",
                "relation",
                "detail",
                "source_hash",
                "evidence_refs",
                "schema_version",
                "canonical_mutation",
                "filesystem_mutation",
            }
        ),
    )
    try:
        return StructuralFinding(**dict(record))
    except (TypeError, ValueError) as exc:
        raise StructuralExportReportInspectionError(
            f"policy finding을 복원하지 못했습니다: {exc}",
            error_kind="CONTRACT_REJECTED",
        ) from exc


def _parse_policy_report(value: Any) -> StructuralPolicyReport:
    record = _strict_mapping(
        value,
        "review.policy_report",
        keys=frozenset(
            {
                "id",
                "report_hash",
                "source_id",
                "source_hash",
                "status",
                "rules",
                "findings",
                "metadata",
                "schema_version",
                "canonical_mutation",
                "filesystem_mutation",
            }
        ),
    )
    try:
        report = StructuralPolicyReport(
            id=record["id"],
            report_hash=record["report_hash"],
            source_id=record["source_id"],
            source_hash=record["source_hash"],
            rules=tuple(
                _parse_rule(item, index)
                for index, item in enumerate(_list(record["rules"], "review.policy_report.rules"))
            ),
            findings=tuple(
                _parse_finding(item, index)
                for index, item in enumerate(
                    _list(record["findings"], "review.policy_report.findings")
                )
            ),
            metadata=record["metadata"],
            schema_version=record["schema_version"],
            canonical_mutation=record["canonical_mutation"],
            filesystem_mutation=record["filesystem_mutation"],
        )
    except (TypeError, ValueError) as exc:
        raise StructuralExportReportInspectionError(
            f"policy report를 복원하지 못했습니다: {exc}",
            error_kind="CONTRACT_REJECTED",
        ) from exc
    if record["status"] != report.to_record()["status"]:
        raise StructuralExportReportInspectionError(
            "policy report status가 findings와 일치하지 않습니다.",
            error_kind="INTEGRITY_FAILED",
        )
    return report


def _parse_policy_gate(value: Any) -> StructuralPolicyGateResult:
    record = _strict_mapping(
        value,
        "review.policy_gate",
        keys=frozenset(
            {
                "schema_version",
                "id",
                "report_id",
                "source_id",
                "source_hash",
                "status",
                "passed",
                "exit_code",
                "blocking_finding_ids",
                "observed_finding_ids",
                "unresolved_finding_ids",
                "canonical_mutation",
                "filesystem_mutation",
            }
        ),
    )
    try:
        gate = StructuralPolicyGateResult(
            schema_version=record["schema_version"],
            id=record["id"],
            report_id=record["report_id"],
            source_id=record["source_id"],
            source_hash=record["source_hash"],
            status=record["status"],
            exit_code=record["exit_code"],
            blocking_finding_ids=tuple(
                _list(record["blocking_finding_ids"], "review.policy_gate.blocking_finding_ids")
            ),
            observed_finding_ids=tuple(
                _list(record["observed_finding_ids"], "review.policy_gate.observed_finding_ids")
            ),
            unresolved_finding_ids=tuple(
                _list(record["unresolved_finding_ids"], "review.policy_gate.unresolved_finding_ids")
            ),
            canonical_mutation=record["canonical_mutation"],
            filesystem_mutation=record["filesystem_mutation"],
        )
    except (TypeError, ValueError) as exc:
        raise StructuralExportReportInspectionError(
            f"policy gate를 복원하지 못했습니다: {exc}",
            error_kind="CONTRACT_REJECTED",
        ) from exc
    if record["passed"] != gate.passed:
        raise StructuralExportReportInspectionError(
            "policy gate passed 값이 status와 일치하지 않습니다.",
            error_kind="INTEGRITY_FAILED",
        )
    return gate


def _parse_review(value: Any) -> StructuralExportReviewResult:
    record = _strict_mapping(
        value,
        "review",
        keys=frozenset(
            {
                "schema_version",
                "id",
                "stages",
                "input",
                "expected_workspace_hash",
                "imported",
                "policy_report",
                "policy_gate",
                "passed",
                "automatic",
                "canonical_mutation",
                "filesystem_mutation",
            }
        ),
    )
    if record["schema_version"] != STRUCTURAL_EXPORT_REVIEW_SCHEMA:
        raise StructuralExportReportInspectionError(
            "지원하지 않는 structural export review schema입니다.",
            error_kind="CONTRACT_REJECTED",
        )
    stages = tuple(_list(record["stages"], "review.stages"))
    if stages != STRUCTURAL_EXPORT_REVIEW_STAGES:
        raise StructuralExportReportInspectionError(
            "structural export review 단계 순서가 계약과 다릅니다.",
            error_kind="INTEGRITY_FAILED",
        )
    input_record = _strict_mapping(
        record["input"],
        "review.input",
        keys=frozenset({"kind", "path", "size_bytes", "sha256"}),
    )
    imported_record = _strict_mapping(
        record["imported"],
        "review.imported",
        keys=frozenset({"observation", "receipt"}),
    )
    receipt_record = _strict_mapping(
        imported_record["receipt"],
        "review.imported.receipt",
        keys=frozenset(
            {
                "schema_version",
                "id",
                "adapter_id",
                "adapter_version",
                "observation_id",
                "observation_hash",
                "source_id",
                "workspace_hash",
                "canonical_mutation",
                "filesystem_mutation",
            }
        ),
    )
    try:
        observation = parse_structural_observation(imported_record["observation"])
        receipt = StructuralImportReceipt(**dict(receipt_record))
        imported = StructuralImport(observation=observation, receipt=receipt)
        review = StructuralExportReviewResult(
            schema_version=record["schema_version"],
            id=record["id"],
            input_kind=input_record["kind"],
            input_path=input_record["path"],
            input_size_bytes=input_record["size_bytes"],
            input_sha256=input_record["sha256"],
            expected_workspace_hash=record["expected_workspace_hash"],
            stages=stages,
            imported=imported,
            policy_report=_parse_policy_report(record["policy_report"]),
            policy_gate=_parse_policy_gate(record["policy_gate"]),
            automatic=record["automatic"],
            canonical_mutation=record["canonical_mutation"],
            filesystem_mutation=record["filesystem_mutation"],
        )
    except TypeError as exc:
        raise StructuralExportReportInspectionError(
            f"review contract를 복원하지 못했습니다: {exc}",
            error_kind="CONTRACT_REJECTED",
        ) from exc
    except ValueError as exc:
        raise StructuralExportReportInspectionError(
            f"review contract 무결성 검증에 실패했습니다: {exc}",
            error_kind="INTEGRITY_FAILED",
        ) from exc
    if record["passed"] != review.passed:
        raise StructuralExportReportInspectionError(
            "review passed 값이 policy gate와 일치하지 않습니다.",
            error_kind="INTEGRITY_FAILED",
        )
    return review


def _parse_report(value: Any) -> StructuralExportReportArtifact:
    record = _strict_mapping(
        value,
        "report",
        keys=frozenset(
            {
                "schema_version",
                "id",
                "kind",
                "output",
                "review_id",
                "review",
                "passed",
                "gate_exit_code",
                "automatic",
                "output_file_written",
                "filesystem_mutation",
                "source_mutation",
                "workspace_mutation",
                "canonical_mutation",
            }
        ),
    )
    if record["schema_version"] != STRUCTURAL_EXPORT_REPORT_SCHEMA:
        raise StructuralExportReportInspectionError(
            "지원하지 않는 structural export report schema입니다.",
            error_kind="CONTRACT_REJECTED",
        )
    if record["kind"] != "derived_structural_export_review_report":
        raise StructuralExportReportInspectionError(
            "report kind가 올바르지 않습니다.",
            error_kind="CONTRACT_REJECTED",
        )
    output = _strict_mapping(
        record["output"],
        "report.output",
        keys=frozenset({"path", "overwrote_existing"}),
    )
    review = _parse_review(record["review"])
    if record["review_id"] != review.id:
        raise StructuralExportReportInspectionError(
            "report.review_id가 nested review와 일치하지 않습니다.",
            error_kind="INTEGRITY_FAILED",
        )
    try:
        artifact = StructuralExportReportArtifact(
            review=review,
            output_path=output["path"],
            schema_version=record["schema_version"],
            id=record["id"],
            automatic=record["automatic"],
            output_file_written=record["output_file_written"],
            filesystem_mutation=record["filesystem_mutation"],
            source_mutation=record["source_mutation"],
            workspace_mutation=record["workspace_mutation"],
            canonical_mutation=record["canonical_mutation"],
            overwrote_existing=output["overwrote_existing"],
        )
    except (TypeError, ValueError) as exc:
        raise StructuralExportReportInspectionError(
            f"report contract 무결성 검증에 실패했습니다: {exc}",
            error_kind="INTEGRITY_FAILED",
        ) from exc
    if record["passed"] != artifact.passed:
        raise StructuralExportReportInspectionError(
            "report passed 값이 nested review와 일치하지 않습니다.",
            error_kind="INTEGRITY_FAILED",
        )
    if record["gate_exit_code"] != artifact.gate_exit_code:
        raise StructuralExportReportInspectionError(
            "report gate_exit_code가 nested policy gate와 일치하지 않습니다.",
            error_kind="INTEGRITY_FAILED",
        )
    if dict(record) != artifact.to_record():
        raise StructuralExportReportInspectionError(
            "report record가 immutable contract의 canonical record와 다릅니다.",
            error_kind="INTEGRITY_FAILED",
        )
    return artifact


def _read_report_file(path: str | Path) -> tuple[str, bytes, Mapping[str, Any]]:
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError) as exc:
        raise StructuralExportReportInspectionError(
            "report 파일 경로를 해석할 수 없습니다.",
            error_kind="FILE_READ_FAILED",
        ) from exc
    display_path = _text(str(candidate), "report_path")
    if not candidate.is_file():
        raise StructuralExportReportInspectionError(
            "report 파일을 찾을 수 없습니다.",
            error_kind="FILE_READ_FAILED",
        )
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise StructuralExportReportInspectionError(
            "report 파일을 읽을 수 없습니다.",
            error_kind="FILE_READ_FAILED",
        ) from exc
    if not raw:
        raise StructuralExportReportInspectionError(
            "report 파일이 비어 있습니다.",
            error_kind="INVALID_INPUT",
        )
    if len(raw) > MAX_STRUCTURAL_EXPORT_REPORT_BYTES:
        raise StructuralExportReportInspectionError(
            f"report가 허용 크기({MAX_STRUCTURAL_EXPORT_REPORT_BYTES} bytes)를 초과했습니다.",
            error_kind="REPORT_TOO_LARGE",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuralExportReportInspectionError(
            "report 파일이 유효한 UTF-8이 아닙니다.",
            error_kind="INVALID_UTF8",
        ) from exc
    try:
        value = json.loads(text, object_pairs_hook=_duplicate_key_rejector)
    except _DuplicateJsonKeyError as exc:
        raise StructuralExportReportInspectionError(
            f"report JSON에 중복 key가 있습니다: {exc}",
            error_kind="DUPLICATE_JSON_KEY",
        ) from exc
    except json.JSONDecodeError as exc:
        raise StructuralExportReportInspectionError(
            "report가 유효한 JSON이 아닙니다.",
            error_kind="INVALID_INPUT",
        ) from exc
    return display_path, raw, _mapping(value, "report")


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralExportReportInspection:
    """Immutable read-only verification projection for one report file."""

    report: StructuralExportReportArtifact
    report_path: str
    report_file_sha256: str
    report_size_bytes: int
    schema_version: str = STRUCTURAL_EXPORT_REPORT_INSPECTION_SCHEMA
    id: str = ""
    automatic: bool = False
    filesystem_mutation: bool = False
    source_mutation: bool = False
    workspace_mutation: bool = False
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_EXPORT_REPORT_INSPECTION_SCHEMA:
            raise StructuralExportReportInspectionError(
                "지원하지 않는 structural export report inspection schema입니다.",
                error_kind="CONTRACT_REJECTED",
            )
        if not isinstance(self.report, StructuralExportReportArtifact):
            raise StructuralExportReportInspectionError(
                "inspection.report 타입이 잘못되었습니다.",
                error_kind="CONTRACT_REJECTED",
            )
        report_path = _text(self.report_path, "inspection.report_path")
        object.__setattr__(self, "report_path", report_path)
        digest = _text(self.report_file_sha256, "inspection.report_file_sha256", limit=80).lower()
        if (
            not digest.startswith("sha256:")
            or len(digest) != len("sha256:") + 64
            or any(char not in "0123456789abcdef" for char in digest.removeprefix("sha256:"))
        ):
            raise StructuralExportReportInspectionError(
                "inspection.report_file_sha256가 올바른 sha256 digest가 아닙니다.",
                error_kind="CONTRACT_REJECTED",
            )
        object.__setattr__(self, "report_file_sha256", digest)
        if (
            isinstance(self.report_size_bytes, bool)
            or not isinstance(self.report_size_bytes, int)
            or self.report_size_bytes < 1
            or self.report_size_bytes > MAX_STRUCTURAL_EXPORT_REPORT_BYTES
        ):
            raise StructuralExportReportInspectionError(
                "inspection.report_size_bytes 범위가 올바르지 않습니다.",
                error_kind="CONTRACT_REJECTED",
            )
        for value, label in (
            (self.automatic, "inspection.automatic"),
            (self.filesystem_mutation, "inspection.filesystem_mutation"),
            (self.source_mutation, "inspection.source_mutation"),
            (self.workspace_mutation, "inspection.workspace_mutation"),
            (self.canonical_mutation, "inspection.canonical_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise StructuralExportReportInspectionError(
                    f"{label}은(는) false여야 합니다.",
                    error_kind="CONTRACT_REJECTED",
                )
        inspection_hash = canonical_hash(self._hash_payload())
        expected_id = (
            f"structural-export-report-inspection:{inspection_hash.removeprefix('sha256:')}"
        )
        if self.id and self.id != expected_id:
            raise StructuralExportReportInspectionError(
                "inspection id가 payload와 일치하지 않습니다.",
                error_kind="INTEGRITY_FAILED",
            )
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report.id,
            "report_file_sha256": self.report_file_sha256,
            "report_size_bytes": self.report_size_bytes,
        }

    @property
    def passed(self) -> bool:
        return self.report.passed

    @property
    def gate_exit_code(self) -> int:
        return self.report.gate_exit_code

    def to_record(self) -> dict[str, object]:
        review = self.report.review
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": "read_only_structural_export_report_inspection",
            "valid": True,
            "input": {
                "kind": "explicit_file",
                "path": self.report_path,
                "size_bytes": self.report_size_bytes,
                "sha256": self.report_file_sha256,
            },
            "report_id": self.report.id,
            "review_id": review.id,
            "source": {
                "input_kind": review.input_kind,
                "input_sha256": review.input_sha256,
                "workspace_hash": review.observation.workspace_hash,
                "observation_hash": review.observation.observation_hash,
            },
            "gate": {
                "status": review.policy_gate.status,
                "passed": self.passed,
                "exit_code": self.gate_exit_code,
            },
            "report_output": {
                "path": self.report.output_path,
                "historical_output_file_written": self.report.output_file_written,
                "historical_filesystem_mutation": self.report.filesystem_mutation,
            },
            "automatic": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def inspect_structural_export_report_file(
    path: str | Path,
) -> StructuralExportReportInspection:
    """Read and verify one explicit Phase 50 report without writing anything."""

    report_path, raw, record = _read_report_file(path)
    try:
        report = _parse_report(record)
    except StructuralExportReportInspectionError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise StructuralExportReportInspectionError(
            f"report contract 검증에 실패했습니다: {exc}",
            error_kind="CONTRACT_REJECTED",
        ) from exc
    return StructuralExportReportInspection(
        report=report,
        report_path=report_path,
        report_file_sha256=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        report_size_bytes=len(raw),
    )


def _error_record(
    error: StructuralExportReportInspectionError,
    path: Path,
) -> dict[str, object]:
    try:
        report_path = str(path.expanduser().resolve(strict=False))
    except (OSError, TypeError, ValueError):
        report_path = str(path)
    return {
        "schema_version": STRUCTURAL_EXPORT_REPORT_INSPECTION_SCHEMA,
        "status": "ERROR",
        "valid": False,
        "input": {"kind": "explicit_file", "path": report_path},
        "error_kind": error.error_kind,
        "error": str(error),
        "automatic": False,
        "filesystem_mutation": False,
        "source_mutation": False,
        "workspace_mutation": False,
        "canonical_mutation": False,
    }


def _emit(text: str) -> None:
    """Write UTF-8 bytes to stdout regardless of the console code page.

    These records carry Korean reasons; ``print`` encodes through the console
    encoding, so on a cp949 terminal the ordinary success output becomes bytes
    that are not valid UTF-8 and anything piping this CLI into a JSON parser
    fails on the normal path. Mirrors ``structural_dispatch._emit`` (Phase 59).
    """

    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect one explicit structural export report without writing or re-reviewing it."
    )
    parser.add_argument("--report-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        inspection = inspect_structural_export_report_file(args.report_file)
    except StructuralExportReportInspectionError as exc:
        _emit(
            json.dumps(
                _error_record(exc, args.report_file),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return STRUCTURAL_EXPORT_REPORT_INSPECTION_ERROR_EXIT_CODE
    _emit(inspection.to_json())
    return inspection.gate_exit_code


__all__ = [
    "MAX_STRUCTURAL_EXPORT_REPORT_BYTES",
    "STRUCTURAL_EXPORT_REPORT_INSPECTION_ERROR_EXIT_CODE",
    "STRUCTURAL_EXPORT_REPORT_INSPECTION_SCHEMA",
    "StructuralExportReportInspection",
    "StructuralExportReportInspectionError",
    "inspect_structural_export_report_file",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
