"""Explicit materialization of one structural export review result.

Phase 48 owns the review pipeline.  This module only materializes its derived
result when both the input export and the output report path are explicitly
provided by the caller.  It never discovers a sensor, stores the raw export,
or turns the report back into state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_export_review import (
    STRUCTURAL_EXPORT_REVIEW_STAGES,
    StructuralExportReviewError,
    StructuralExportReviewResult,
    review_structural_export_file,
)

STRUCTURAL_EXPORT_REPORT_SCHEMA = "structural.export.report.v1"
STRUCTURAL_EXPORT_REPORT_ERROR_EXIT_CODE = 5
_MAX_PATH_TEXT = 4_096
_ERROR_KINDS = frozenset(
    {
        "INVALID_OUTPUT",
        "REPORT_EXISTS",
        "REPORT_PATH_CONFLICT",
        "REPORT_PARENT_MISSING",
        "REPORT_WRITE_FAILED",
    }
)


class StructuralExportReportError(ValueError):
    """Raised when an explicit derived report cannot be materialized safely."""

    def __init__(self, message: str, *, error_kind: str = "INVALID_OUTPUT") -> None:
        normalized_kind = str(error_kind).strip().upper()
        if normalized_kind not in _ERROR_KINDS:
            normalized_kind = "INVALID_OUTPUT"
        self.error_kind = normalized_kind
        super().__init__(message)


def _text(value: Any, label: str, *, limit: int = _MAX_PATH_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuralExportReportError(f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.")
    result = value.strip()
    if "\x00" in result:
        raise StructuralExportReportError(f"{label}에 허용되지 않은 NUL 문자가 있습니다.")
    if len(result) > limit:
        raise StructuralExportReportError(f"{label}이(가) 너무 깁니다.")
    return result


def _resolve_output_path(path: str | Path) -> Path:
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError) as exc:
        raise StructuralExportReportError(
            "report 출력 경로를 해석할 수 없습니다.",
            error_kind="INVALID_OUTPUT",
        ) from exc
    _text(str(candidate), "report_output_path")
    return candidate


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralExportReportArtifact:
    """Immutable, non-authoritative report projection for one review result."""

    review: StructuralExportReviewResult
    output_path: str
    schema_version: str = STRUCTURAL_EXPORT_REPORT_SCHEMA
    id: str = ""
    automatic: bool = False
    output_file_written: bool = True
    filesystem_mutation: bool = True
    source_mutation: bool = False
    workspace_mutation: bool = False
    canonical_mutation: bool = False
    overwrote_existing: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_EXPORT_REPORT_SCHEMA:
            raise StructuralExportReportError("지원하지 않는 structural export report schema입니다.")
        if not isinstance(self.review, StructuralExportReviewResult):
            raise StructuralExportReportError("report.review 타입이 잘못되었습니다.")
        output_path = _text(self.output_path, "report.output_path")
        object.__setattr__(self, "output_path", output_path)
        if self.review.input_path is not None:
            try:
                input_path = Path(self.review.input_path).resolve(strict=False)
                output = Path(output_path).resolve(strict=False)
            except (OSError, TypeError, ValueError) as exc:
                raise StructuralExportReportError(
                    "report 입력·출력 경로를 비교할 수 없습니다.",
                    error_kind="REPORT_PATH_CONFLICT",
                ) from exc
            if input_path == output:
                raise StructuralExportReportError(
                    "report 출력 경로는 export 입력 경로와 달라야 합니다.",
                    error_kind="REPORT_PATH_CONFLICT",
                )
        for value, label in (
            (self.automatic, "report.automatic"),
            (self.output_file_written, "report.output_file_written"),
            (self.filesystem_mutation, "report.filesystem_mutation"),
            (self.source_mutation, "report.source_mutation"),
            (self.workspace_mutation, "report.workspace_mutation"),
            (self.canonical_mutation, "report.canonical_mutation"),
            (self.overwrote_existing, "report.overwrote_existing"),
        ):
            if not isinstance(value, bool):
                raise StructuralExportReportError(f"{label}은(는) boolean이어야 합니다.")
        if self.automatic or not self.output_file_written or not self.filesystem_mutation:
            raise StructuralExportReportError(
                "명시적 report artifact의 write 상태가 계약과 다릅니다."
            )
        if self.source_mutation or self.workspace_mutation or self.canonical_mutation:
            raise StructuralExportReportError("report artifact는 source/workspace/Canonical을 변경할 수 없습니다.")
        if self.overwrote_existing:
            raise StructuralExportReportError("report artifact는 기존 파일을 덮어쓸 수 없습니다.")

        report_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-export-report:{report_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralExportReportError("structural export report id가 payload와 다릅니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "review_id": self.review.id,
            "review_input_sha256": self.review.input_sha256,
            "review_stages": list(STRUCTURAL_EXPORT_REVIEW_STAGES),
            "gate_status": self.review.policy_gate.status,
            "gate_exit_code": self.review.policy_gate.exit_code,
        }

    @property
    def passed(self) -> bool:
        return self.review.passed

    @property
    def gate_exit_code(self) -> int:
        return self.review.policy_gate.exit_code

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": "derived_structural_export_review_report",
            "output": {
                "path": self.output_path,
                "overwrote_existing": False,
            },
            "review_id": self.review.id,
            "review": self.review.to_record(),
            "passed": self.passed,
            "gate_exit_code": self.gate_exit_code,
            "automatic": False,
            "output_file_written": True,
            "filesystem_mutation": True,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def materialize_structural_export_report(
    review: StructuralExportReviewResult,
    output_path: str | Path,
) -> StructuralExportReportArtifact:
    """Atomically write one derived report to a caller-selected new file."""

    if not isinstance(review, StructuralExportReviewResult):
        raise StructuralExportReportError("report review 타입이 잘못되었습니다.")
    resolved_output = _resolve_output_path(output_path)
    if review.input_path is not None:
        try:
            if Path(review.input_path).resolve(strict=False) == resolved_output:
                raise StructuralExportReportError(
                    "report 출력 경로는 export 입력 경로와 달라야 합니다.",
                    error_kind="REPORT_PATH_CONFLICT",
                )
        except (OSError, TypeError, ValueError) as exc:
            if isinstance(exc, StructuralExportReportError):
                raise
            raise StructuralExportReportError(
                "report 입력·출력 경로를 비교할 수 없습니다.",
                error_kind="REPORT_PATH_CONFLICT",
            ) from exc
    if resolved_output.exists():
        raise StructuralExportReportError(
            "기존 report 파일은 덮어쓰지 않습니다.",
            error_kind="REPORT_EXISTS",
        )
    if not resolved_output.parent.is_dir():
        raise StructuralExportReportError(
            "report 출력 부모 디렉터리가 존재하지 않습니다.",
            error_kind="REPORT_PARENT_MISSING",
        )

    artifact = StructuralExportReportArtifact(
        review=review,
        output_path=str(resolved_output),
    )
    payload = f"{artifact.to_json()}\n".encode()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(resolved_output.parent),
            prefix=f".{resolved_output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if resolved_output.exists():
            raise StructuralExportReportError(
                "기존 report 파일은 덮어쓰지 않습니다.",
                error_kind="REPORT_EXISTS",
            )
        os.replace(temporary_path, resolved_output)
    except StructuralExportReportError:
        raise
    except (OSError, UnicodeError) as exc:
        raise StructuralExportReportError(
            "report 파일을 원자적으로 쓸 수 없습니다.",
            error_kind="REPORT_WRITE_FAILED",
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return artifact


def _error_record(
    error: StructuralExportReportError,
    export_path: Path,
    report_path: Path,
) -> dict[str, object]:
    try:
        input_path = str(export_path.expanduser().resolve(strict=False))
    except (OSError, TypeError, ValueError):
        input_path = str(export_path)
    try:
        output_path = str(report_path.expanduser().resolve(strict=False))
    except (OSError, TypeError, ValueError):
        output_path = str(report_path)
    return {
        "schema_version": STRUCTURAL_EXPORT_REPORT_SCHEMA,
        "status": "ERROR",
        "passed": False,
        "input": {"kind": "explicit_file", "path": input_path},
        "output": {"path": output_path},
        "error_kind": error.error_kind,
        "error": str(error),
        "automatic": False,
        "output_file_written": False,
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
        description="Materialize one explicit structural export review as a derived report."
    )
    parser.add_argument("--export-file", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    parser.add_argument("--expected-workspace-hash")
    args = parser.parse_args(argv)
    try:
        review = review_structural_export_file(
            args.export_file,
            expected_workspace_hash=args.expected_workspace_hash,
        )
        artifact = materialize_structural_export_report(review, args.report_file)
    except StructuralExportReviewError as exc:
        _emit(
            json.dumps(
                {
                    "schema_version": STRUCTURAL_EXPORT_REPORT_SCHEMA,
                    "status": "ERROR",
                    "passed": False,
                    "error_kind": exc.error_kind,
                    "error": str(exc),
                    "automatic": False,
                    "output_file_written": False,
                    "filesystem_mutation": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 4
    except StructuralExportReportError as exc:
        _emit(
            json.dumps(
                _error_record(exc, args.export_file, args.report_file),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return STRUCTURAL_EXPORT_REPORT_ERROR_EXIT_CODE
    _emit(artifact.to_json())
    return artifact.gate_exit_code


__all__ = [
    "STRUCTURAL_EXPORT_REPORT_ERROR_EXIT_CODE",
    "STRUCTURAL_EXPORT_REPORT_SCHEMA",
    "StructuralExportReportArtifact",
    "StructuralExportReportError",
    "main",
    "materialize_structural_export_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
