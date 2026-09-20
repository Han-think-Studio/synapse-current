"""Explicit read-only review of one supplied Infigraph JSON export.

Phase 40, 38, and 42 already provide the adapter, policy evaluator, and
policy gate as independent contracts.  This module is the deliberately small
caller-facing boundary that composes them for one file.  It never starts a
sensor, discovers a command, watches a workspace, or writes a result file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.infigraph_adapter import InfigraphAdapterError, InfigraphJsonAdapter
from synapse.core.structural_adapter import StructuralImport
from synapse.core.structural_policy import (
    BUILT_IN_STRUCTURAL_POLICY_RULES,
    StructuralPolicyReport,
    StructuralPolicyRule,
    evaluate_structural_policy,
)
from synapse.core.structural_policy_gate import (
    StructuralPolicyGateResult,
    evaluate_structural_policy_gate,
)

STRUCTURAL_EXPORT_REVIEW_SCHEMA = "structural.export.review.v1"
STRUCTURAL_EXPORT_REVIEW_STAGES = (
    "EXPORT_READ",
    "OBSERVATION_IMPORTED",
    "POLICY_EVALUATED",
    "POLICY_GATED",
)
STRUCTURAL_EXPORT_REVIEW_ERROR_EXIT_CODE = 4
MAX_STRUCTURAL_EXPORT_BYTES = 8 * 1024 * 1024
_MAX_TEXT = 4_000
_MAX_PATH_TEXT = 4_096
_ERROR_KINDS = frozenset(
    {
        "INVALID_INPUT",
        "FILE_READ_FAILED",
        "INVALID_UTF8",
        "EXPORT_TOO_LARGE",
        "IMPORT_REJECTED",
        "STALE_WORKSPACE",
        "POLICY_REJECTED",
        "GATE_REJECTED",
    }
)


class StructuralExportReviewError(ValueError):
    """Raised when a supplied export cannot be reviewed safely."""

    def __init__(self, message: str, *, error_kind: str = "INVALID_INPUT") -> None:
        normalized_kind = str(error_kind).strip().upper()
        if normalized_kind not in _ERROR_KINDS:
            normalized_kind = "INVALID_INPUT"
        self.error_kind = normalized_kind
        super().__init__(message)


def _text(value: Any, label: str, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuralExportReviewError(f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.")
    result = value.strip()
    if "\x00" in result:
        raise StructuralExportReviewError(f"{label}에 허용되지 않은 NUL 문자가 있습니다.")
    if len(result) > limit:
        raise StructuralExportReviewError(f"{label}이(가) 너무 깁니다.")
    return result


def _optional_text(value: Any, label: str, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    return _text(value, label, limit=limit)


def _digest(value: Any, label: str) -> str:
    result = _text(value, label, limit=80).lower()
    if (
        not result.startswith("sha256:")
        or len(result) != len("sha256:") + 64
        or any(char not in "0123456789abcdef" for char in result.removeprefix("sha256:"))
    ):
        raise StructuralExportReviewError(f"{label}가 올바른 sha256 digest가 아닙니다.")
    return result


def _input_bytes(value: Mapping[str, Any] | str) -> bytes:
    if isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StructuralExportReviewError(
                "inline export를 UTF-8로 인코딩할 수 없습니다.",
                error_kind="INVALID_UTF8",
            ) from exc
    elif isinstance(value, Mapping):
        try:
            raw = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, UnicodeEncodeError, ValueError) as exc:
            raise StructuralExportReviewError(
                "inline export는 JSON 값만 포함해야 합니다.",
                error_kind="INVALID_INPUT",
            ) from exc
    else:
        raise StructuralExportReviewError("export는 JSON 객체 또는 JSON 문자열이어야 합니다.")
    if not raw:
        raise StructuralExportReviewError("export가 비어 있습니다.")
    if len(raw) > MAX_STRUCTURAL_EXPORT_BYTES:
        raise StructuralExportReviewError(
            f"export가 허용 크기({MAX_STRUCTURAL_EXPORT_BYTES} bytes)를 초과했습니다.",
            error_kind="EXPORT_TOO_LARGE",
        )
    return raw


def _read_export_file(path: str | Path) -> tuple[str, bytes, str]:
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError) as exc:
        raise StructuralExportReviewError(
            "export 파일 경로를 해석할 수 없습니다.",
            error_kind="FILE_READ_FAILED",
        ) from exc
    display_path = _text(str(candidate), "export_path", limit=_MAX_PATH_TEXT)
    if not candidate.is_file():
        raise StructuralExportReviewError(
            "export 파일을 찾을 수 없습니다.",
            error_kind="FILE_READ_FAILED",
        )
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise StructuralExportReviewError(
            "export 파일을 읽을 수 없습니다.",
            error_kind="FILE_READ_FAILED",
        ) from exc
    if not raw:
        raise StructuralExportReviewError("export 파일이 비어 있습니다.", error_kind="INVALID_INPUT")
    if len(raw) > MAX_STRUCTURAL_EXPORT_BYTES:
        raise StructuralExportReviewError(
            f"export가 허용 크기({MAX_STRUCTURAL_EXPORT_BYTES} bytes)를 초과했습니다.",
            error_kind="EXPORT_TOO_LARGE",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuralExportReviewError(
            "export 파일이 유효한 UTF-8이 아닙니다.",
            error_kind="INVALID_UTF8",
        ) from exc
    return text, raw, display_path


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralExportReviewResult:
    """Immutable composition result for one supplied export review."""

    input_kind: str
    input_sha256: str
    input_size_bytes: int
    imported: StructuralImport
    policy_report: StructuralPolicyReport
    policy_gate: StructuralPolicyGateResult
    input_path: str | None = None
    expected_workspace_hash: str | None = None
    stages: tuple[str, ...] = STRUCTURAL_EXPORT_REVIEW_STAGES
    schema_version: str = STRUCTURAL_EXPORT_REVIEW_SCHEMA
    id: str = ""
    automatic: bool = False
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_EXPORT_REVIEW_SCHEMA:
            raise StructuralExportReviewError("지원하지 않는 structural export review schema입니다.")
        input_kind = _text(self.input_kind, "review.input_kind", limit=40).lower()
        if input_kind not in {"inline", "explicit_file"}:
            raise StructuralExportReviewError("review.input_kind가 올바르지 않습니다.")
        object.__setattr__(self, "input_kind", input_kind)
        object.__setattr__(self, "input_sha256", _digest(self.input_sha256, "review.input_sha256"))
        if (
            isinstance(self.input_size_bytes, bool)
            or not isinstance(self.input_size_bytes, int)
            or self.input_size_bytes < 1
            or self.input_size_bytes > MAX_STRUCTURAL_EXPORT_BYTES
        ):
            raise StructuralExportReviewError("review.input_size_bytes 범위가 올바르지 않습니다.")
        if not isinstance(self.imported, StructuralImport):
            raise StructuralExportReviewError("review.imported 타입이 잘못되었습니다.")
        if not isinstance(self.policy_report, StructuralPolicyReport):
            raise StructuralExportReviewError("review.policy_report 타입이 잘못되었습니다.")
        if not isinstance(self.policy_gate, StructuralPolicyGateResult):
            raise StructuralExportReviewError("review.policy_gate 타입이 잘못되었습니다.")
        stages = tuple(self.stages)
        if stages != STRUCTURAL_EXPORT_REVIEW_STAGES:
            raise StructuralExportReviewError("structural export review 단계 순서가 계약과 다릅니다.")
        object.__setattr__(self, "stages", stages)
        input_path = _optional_text(self.input_path, "review.input_path", limit=_MAX_PATH_TEXT)
        if input_kind == "explicit_file" and input_path is None:
            raise StructuralExportReviewError("explicit_file review에는 input_path가 필요합니다.")
        if input_kind == "inline" and input_path is not None:
            raise StructuralExportReviewError("inline review에는 input_path를 둘 수 없습니다.")
        object.__setattr__(self, "input_path", input_path)
        expected_workspace_hash = _optional_text(
            self.expected_workspace_hash,
            "review.expected_workspace_hash",
            limit=240,
        )
        object.__setattr__(self, "expected_workspace_hash", expected_workspace_hash)

        observation = self.imported.observation
        if (
            self.policy_report.source_id != observation.source_id
            or self.policy_report.source_hash != observation.observation_hash
        ):
            raise StructuralExportReviewError("policy report identity가 imported observation과 다릅니다.")
        if (
            self.policy_gate.report_id != self.policy_report.id
            or self.policy_gate.source_id != self.policy_report.source_id
            or self.policy_gate.source_hash != self.policy_report.source_hash
        ):
            raise StructuralExportReviewError("policy gate identity가 policy report와 다릅니다.")
        if (
            expected_workspace_hash is not None
            and observation.workspace_hash != expected_workspace_hash
        ):
            raise StructuralExportReviewError(
                "expected workspace hash가 imported observation과 다릅니다.",
                error_kind="STALE_WORKSPACE",
            )
        for value, label in (
            (self.automatic, "review.automatic"),
            (self.canonical_mutation, "review.canonical_mutation"),
            (self.filesystem_mutation, "review.filesystem_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise StructuralExportReviewError(f"{label}은(는) false여야 합니다.")

        review_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-export-review:{review_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise StructuralExportReviewError("structural export review id가 payload와 다릅니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "input_kind": self.input_kind,
            "input_sha256": self.input_sha256,
            "input_size_bytes": self.input_size_bytes,
            "import_receipt_id": self.imported.receipt.id,
            "observation_id": self.imported.observation.id,
            "observation_hash": self.imported.observation.observation_hash,
            "policy_report_id": self.policy_report.id,
            "policy_report_hash": self.policy_report.report_hash,
            "policy_gate_id": self.policy_gate.id,
            "policy_gate_status": self.policy_gate.status,
            "policy_gate_exit_code": self.policy_gate.exit_code,
            "expected_workspace_hash": self.expected_workspace_hash,
            "stages": list(self.stages),
        }

    @property
    def observation(self):
        """Return the imported observation without copying or mutating it."""

        return self.imported.observation

    @property
    def passed(self) -> bool:
        return self.policy_gate.passed

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "stages": list(self.stages),
            "input": {
                "kind": self.input_kind,
                "path": self.input_path,
                "size_bytes": self.input_size_bytes,
                "sha256": self.input_sha256,
            },
            "expected_workspace_hash": self.expected_workspace_hash,
            "imported": self.imported.to_record(),
            "policy_report": self.policy_report.to_record(),
            "policy_gate": self.policy_gate.to_record(),
            "passed": self.passed,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def _review(
    value: Mapping[str, Any] | str,
    *,
    input_bytes: bytes,
    input_kind: str,
    input_path: str | None,
    expected_workspace_hash: str | None,
    rules: Sequence[StructuralPolicyRule],
) -> StructuralExportReviewResult:
    expected_workspace_hash = _optional_text(
        expected_workspace_hash,
        "expected_workspace_hash",
        limit=240,
    )
    try:
        imported = InfigraphJsonAdapter().import_observation(value)
    except InfigraphAdapterError as exc:
        raise StructuralExportReviewError(
            str(exc),
            error_kind="IMPORT_REJECTED",
        ) from exc

    if (
        expected_workspace_hash is not None
        and imported.observation.workspace_hash != expected_workspace_hash
    ):
        raise StructuralExportReviewError(
            "expected workspace hash가 imported observation과 다릅니다.",
            error_kind="STALE_WORKSPACE",
        )

    try:
        policy_report = evaluate_structural_policy(imported.observation, rules=rules)
    except (TypeError, ValueError) as exc:
        raise StructuralExportReviewError(
            f"structural policy 평가를 완료하지 못했습니다: {exc}",
            error_kind="POLICY_REJECTED",
        ) from exc

    try:
        policy_gate = evaluate_structural_policy_gate(policy_report)
    except (TypeError, ValueError) as exc:
        raise StructuralExportReviewError(
            f"structural policy gate를 완료하지 못했습니다: {exc}",
            error_kind="GATE_REJECTED",
        ) from exc

    return StructuralExportReviewResult(
        input_kind=input_kind,
        input_sha256=f"sha256:{hashlib.sha256(input_bytes).hexdigest()}",
        input_size_bytes=len(input_bytes),
        input_path=input_path,
        imported=imported,
        policy_report=policy_report,
        policy_gate=policy_gate,
        expected_workspace_hash=expected_workspace_hash,
    )


def review_supplied_structural_export(
    value: Mapping[str, Any] | str,
    *,
    expected_workspace_hash: str | None = None,
    rules: Sequence[StructuralPolicyRule] = BUILT_IN_STRUCTURAL_POLICY_RULES,
) -> StructuralExportReviewResult:
    """Review one already-supplied export value without invoking a sensor."""

    input_bytes = _input_bytes(value)
    return _review(
        value,
        input_bytes=input_bytes,
        input_kind="inline",
        input_path=None,
        expected_workspace_hash=expected_workspace_hash,
        rules=rules,
    )


def review_structural_export_file(
    path: str | Path,
    *,
    expected_workspace_hash: str | None = None,
    rules: Sequence[StructuralPolicyRule] = BUILT_IN_STRUCTURAL_POLICY_RULES,
) -> StructuralExportReviewResult:
    """Read and review one explicitly selected UTF-8 export file."""

    text, input_bytes, input_path = _read_export_file(path)
    return _review(
        text,
        input_bytes=input_bytes,
        input_kind="explicit_file",
        input_path=input_path,
        expected_workspace_hash=expected_workspace_hash,
        rules=rules,
    )


def _error_record(error: StructuralExportReviewError, path: Path) -> dict[str, object]:
    try:
        input_path = str(path.expanduser().resolve(strict=False))
    except (OSError, TypeError, ValueError):
        input_path = str(path)
    return {
        "schema_version": STRUCTURAL_EXPORT_REVIEW_SCHEMA,
        "status": "ERROR",
        "passed": False,
        "stages": list(STRUCTURAL_EXPORT_REVIEW_STAGES),
        "input": {"kind": "explicit_file", "path": input_path},
        "error_kind": error.error_kind,
        "error": str(error),
        "automatic": False,
        "canonical_mutation": False,
        "filesystem_mutation": False,
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
        description="Review one supplied Infigraph JSON export without running a sensor."
    )
    parser.add_argument("--export-file", type=Path, required=True)
    parser.add_argument("--expected-workspace-hash")
    args = parser.parse_args(argv)
    try:
        result = review_structural_export_file(
            args.export_file,
            expected_workspace_hash=args.expected_workspace_hash,
        )
    except StructuralExportReviewError as exc:
        _emit(json.dumps(_error_record(exc, args.export_file), ensure_ascii=False, indent=2, sort_keys=True))
        return STRUCTURAL_EXPORT_REVIEW_ERROR_EXIT_CODE
    _emit(result.to_json())
    return result.policy_gate.exit_code


__all__ = [
    "MAX_STRUCTURAL_EXPORT_BYTES",
    "STRUCTURAL_EXPORT_REVIEW_ERROR_EXIT_CODE",
    "STRUCTURAL_EXPORT_REVIEW_SCHEMA",
    "STRUCTURAL_EXPORT_REVIEW_STAGES",
    "StructuralExportReviewError",
    "StructuralExportReviewResult",
    "main",
    "review_structural_export_file",
    "review_supplied_structural_export",
]


if __name__ == "__main__":
    raise SystemExit(main())
