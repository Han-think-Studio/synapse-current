"""Explicit one-shot process acquisition for supplied Infigraph exports.

This module is deliberately narrower than a sensor runner or watcher.  A
caller must provide a complete argv sequence and explicitly invoke the
adapter.  The child process receives no stdin, its stdout is bounded and
translated only through the Phase 40 JSON adapter, and no result is promoted
to Canonical State.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from synapse.core.idea_session import canonical_hash
from synapse.core.infigraph_adapter import InfigraphAdapterError, InfigraphJsonAdapter
from synapse.core.structural_adapter import StructuralImport, StructuralImportReceipt
from synapse.core.structural_observation import StructuralObservation

INFIGRAPH_PROCESS_SCHEMA = "infigraph.process.v1"
INFIGRAPH_PROCESS_ADAPTER_ID = "infigraph-process"
INFIGRAPH_PROCESS_ADAPTER_VERSION = "1"
MAX_PROCESS_TIMEOUT_SECONDS = 300.0
MAX_PROCESS_STDOUT_BYTES = 8 * 1024 * 1024
MAX_PROCESS_STDERR_BYTES = 64 * 1024
MAX_PROCESS_STDERR_EXCERPT_CHARS = 1_024
_MAX_COMMAND_ITEMS = 128
_MAX_COMMAND_TOKEN_CHARS = 4_096
_MAX_COMMAND_BYTES = 64 * 1024
_PIPE_READ_BYTES = 64 * 1024


class InfigraphProcessError(ValueError):
    """Raised when explicit external process acquisition fails closed."""


def _text(value: Any, label: str, *, limit: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InfigraphProcessError(f"{label}은(는) 비어 있지 않은 문자열이어야 합니다.")
    result = value.strip()
    if "\x00" in result:
        raise InfigraphProcessError(f"{label}에 허용되지 않은 NUL 문자가 있습니다.")
    if len(result) > limit:
        raise InfigraphProcessError(f"{label}이(가) 너무 깁니다.")
    return result


def _command(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InfigraphProcessError("command는 완전한 argv 문자열 배열이어야 합니다.")
    command = tuple(value)
    if not command or len(command) > _MAX_COMMAND_ITEMS:
        raise InfigraphProcessError(
            f"command는 1~{_MAX_COMMAND_ITEMS}개의 argv 항목이어야 합니다."
        )
    total_bytes = 0
    for index, token in enumerate(command):
        if not isinstance(token, str) or not token:
            raise InfigraphProcessError(f"command[{index}]는 비어 있지 않은 문자열이어야 합니다.")
        if "\x00" in token:
            raise InfigraphProcessError(f"command[{index}]에 허용되지 않은 NUL 문자가 있습니다.")
        if len(token) > _MAX_COMMAND_TOKEN_CHARS:
            raise InfigraphProcessError(f"command[{index}]이(가) 너무 깁니다.")
        total_bytes += len(token.encode("utf-8"))
    if total_bytes > _MAX_COMMAND_BYTES:
        raise InfigraphProcessError("command argv 전체가 허용된 크기를 초과했습니다.")
    return command


def _bounded_int(value: Any, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise InfigraphProcessError(f"{label}은(는) 1~{maximum} 사이의 정수여야 합니다.")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decode_stderr(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()[:MAX_PROCESS_STDERR_EXCERPT_CHARS]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalSensorProcessRequest:
    """Explicit, immutable input for one external structural-sensor call."""

    request_id: str
    command: tuple[str, ...]
    cwd: str | Path | None = None
    timeout_seconds: float = 30.0
    max_stdout_bytes: int = MAX_PROCESS_STDOUT_BYTES
    max_stderr_bytes: int = MAX_PROCESS_STDERR_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id, "request_id"))
        object.__setattr__(self, "command", _command(self.command))
        if self.cwd is not None:
            cwd = str(self.cwd)
            if not cwd.strip() or "\x00" in cwd or len(cwd) > _MAX_COMMAND_TOKEN_CHARS:
                raise InfigraphProcessError("cwd는 유효한 경로 문자열이어야 합니다.")
            object.__setattr__(self, "cwd", cwd)
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise InfigraphProcessError("timeout_seconds는 숫자여야 합니다.")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_PROCESS_TIMEOUT_SECONDS:
            raise InfigraphProcessError(
                f"timeout_seconds는 0보다 크고 {MAX_PROCESS_TIMEOUT_SECONDS:g} 이하여야 합니다."
            )
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(
            self,
            "max_stdout_bytes",
            _bounded_int(
                self.max_stdout_bytes,
                "max_stdout_bytes",
                maximum=MAX_PROCESS_STDOUT_BYTES,
            ),
        )
        object.__setattr__(
            self,
            "max_stderr_bytes",
            _bounded_int(
                self.max_stderr_bytes,
                "max_stderr_bytes",
                maximum=MAX_PROCESS_STDERR_BYTES,
            ),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "command": list(self.command),
            "cwd": str(self.cwd) if self.cwd is not None else None,
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalProcessResult:
    """The bounded in-memory result returned by the process runner boundary."""

    returncode: int
    stdout: bytes
    stderr: bytes

    def __post_init__(self) -> None:
        if isinstance(self.returncode, bool) or not isinstance(self.returncode, int):
            raise InfigraphProcessError("process returncode는 정수여야 합니다.")
        if not isinstance(self.stdout, bytes) or not isinstance(self.stderr, bytes):
            raise InfigraphProcessError("process stdout/stderr는 bytes여야 합니다.")


class ExternalProcessRunner(Protocol):
    """Callable seam for deterministic tests around the real process boundary."""

    def __call__(self, request: ExternalSensorProcessRequest) -> ExternalProcessResult:
        """Run exactly one explicit request and return captured bytes."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalSensorProcessReceipt:
    """Provenance for one successful external process-to-observation import."""

    request_id: str
    command: tuple[str, ...]
    cwd: str | None
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    stderr_excerpt: str
    observation_id: str
    observation_hash: str
    source_id: str
    workspace_hash: str
    schema_version: str = INFIGRAPH_PROCESS_SCHEMA
    adapter_id: str = INFIGRAPH_PROCESS_ADAPTER_ID
    adapter_version: str = INFIGRAPH_PROCESS_ADAPTER_VERSION
    process_invoked: bool = True
    automatic: bool = False
    canonical_mutation: bool = False
    filesystem_mutation: bool = False
    id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != INFIGRAPH_PROCESS_SCHEMA:
            raise InfigraphProcessError("지원하지 않는 Infigraph process schema입니다.")
        if self.adapter_id != INFIGRAPH_PROCESS_ADAPTER_ID:
            raise InfigraphProcessError("adapter_id는 infigraph-process여야 합니다.")
        if self.adapter_version != INFIGRAPH_PROCESS_ADAPTER_VERSION:
            raise InfigraphProcessError("지원하지 않는 Infigraph process adapter version입니다.")
        object.__setattr__(self, "request_id", _text(self.request_id, "receipt.request_id"))
        object.__setattr__(self, "command", _command(self.command))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", _text(self.cwd, "receipt.cwd", limit=_MAX_COMMAND_TOKEN_CHARS))
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise InfigraphProcessError("receipt.timeout_seconds는 숫자여야 합니다.")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_PROCESS_TIMEOUT_SECONDS:
            raise InfigraphProcessError("receipt.timeout_seconds 범위가 잘못되었습니다.")
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(
            self,
            "max_stdout_bytes",
            _bounded_int(self.max_stdout_bytes, "receipt.max_stdout_bytes", maximum=MAX_PROCESS_STDOUT_BYTES),
        )
        object.__setattr__(
            self,
            "max_stderr_bytes",
            _bounded_int(self.max_stderr_bytes, "receipt.max_stderr_bytes", maximum=MAX_PROCESS_STDERR_BYTES),
        )
        if self.exit_code != 0:
            raise InfigraphProcessError("성공한 process receipt의 exit_code는 0이어야 합니다.")
        for value, label in (
            (self.stdout_sha256, "receipt.stdout_sha256"),
            (self.stderr_sha256, "receipt.stderr_sha256"),
            (self.observation_hash, "receipt.observation_hash"),
        ):
            _text(value, label, limit=240)
        if len(self.stdout_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.stdout_sha256
        ):
            raise InfigraphProcessError("receipt.stdout_sha256가 올바르지 않습니다.")
        if len(self.stderr_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.stderr_sha256
        ):
            raise InfigraphProcessError("receipt.stderr_sha256가 올바르지 않습니다.")
        if not isinstance(self.stderr_excerpt, str):
            raise InfigraphProcessError("receipt.stderr_excerpt는 문자열이어야 합니다.")
        object.__setattr__(self, "stderr_excerpt", self.stderr_excerpt[:MAX_PROCESS_STDERR_EXCERPT_CHARS])
        object.__setattr__(self, "observation_id", _text(self.observation_id, "receipt.observation_id"))
        object.__setattr__(self, "source_id", _text(self.source_id, "receipt.source_id"))
        object.__setattr__(self, "workspace_hash", _text(self.workspace_hash, "receipt.workspace_hash"))
        for value, label in (
            (self.process_invoked, "receipt.process_invoked"),
            (self.automatic, "receipt.automatic"),
            (self.canonical_mutation, "receipt.canonical_mutation"),
            (self.filesystem_mutation, "receipt.filesystem_mutation"),
        ):
            if not isinstance(value, bool):
                raise InfigraphProcessError(f"{label}은(는) boolean이어야 합니다.")
        if not self.process_invoked or self.automatic or self.canonical_mutation or self.filesystem_mutation:
            raise InfigraphProcessError("process receipt의 실행·변경 상태가 계약과 다릅니다.")

        receipt_hash = canonical_hash(self._hash_payload())
        expected_id = f"external-sensor-process:{receipt_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise InfigraphProcessError("process receipt id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "request_id": self.request_id,
            "command": list(self.command),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "exit_code": self.exit_code,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "observation_id": self.observation_id,
            "observation_hash": self.observation_hash,
            "source_id": self.source_id,
            "workspace_hash": self.workspace_hash,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "request_id": self.request_id,
            "command": list(self.command),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "exit_code": self.exit_code,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stderr_excerpt": self.stderr_excerpt,
            "observation_id": self.observation_id,
            "observation_hash": self.observation_hash,
            "source_id": self.source_id,
            "workspace_hash": self.workspace_hash,
            "process_invoked": True,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalSensorImport:
    """An imported structural observation plus explicit process provenance."""

    structural_import: StructuralImport
    process_receipt: ExternalSensorProcessReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.structural_import, StructuralImport):
            raise InfigraphProcessError("structural_import 타입이 잘못되었습니다.")
        if not isinstance(self.process_receipt, ExternalSensorProcessReceipt):
            raise InfigraphProcessError("process_receipt 타입이 잘못되었습니다.")
        observation = self.structural_import.observation
        receipt = self.process_receipt
        if receipt.observation_id != observation.id or receipt.observation_hash != observation.observation_hash:
            raise InfigraphProcessError("process receipt와 observation identity가 다릅니다.")
        if receipt.source_id != observation.source_id or receipt.workspace_hash != observation.workspace_hash:
            raise InfigraphProcessError("process receipt와 observation provenance가 다릅니다.")

    @property
    def observation(self) -> StructuralObservation:
        return self.structural_import.observation

    @property
    def structural_receipt(self) -> StructuralImportReceipt:
        return self.structural_import.receipt

    def to_record(self) -> dict[str, object]:
        return {
            "structural_import": self.structural_import.to_record(),
            "process_receipt": self.process_receipt.to_record(),
        }


def _read_pipe(
    stream: Any,
    *,
    label: str,
    limit: int,
    chunks: list[bytes],
    overflow_event: threading.Event,
    overflow_labels: list[str],
) -> None:
    """Read one child pipe without retaining bytes beyond its declared limit."""

    total = 0
    try:
        while True:
            chunk = stream.read(_PIPE_READ_BYTES)
            if not chunk:
                return
            if not isinstance(chunk, bytes):
                overflow_labels.append(f"{label}가 bytes가 아닙니다")
                overflow_event.set()
                return
            total += len(chunk)
            if total > limit:
                overflow_labels.append(label)
                overflow_event.set()
                return
            chunks.append(chunk)
    except (OSError, ValueError):
        return


def _close_pipe(stream: Any) -> None:
    try:
        stream.close()
    except (AttributeError, OSError, ValueError):
        return


def _run_process(request: ExternalSensorProcessRequest) -> ExternalProcessResult:
    """Run one argv request without a shell, stdin, retry, or background task."""

    try:
        process = subprocess.Popen(
            list(request.command),
            cwd=request.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise InfigraphProcessError(f"external sensor process를 실행하지 못했습니다: {exc}") from exc

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    overflow_event = threading.Event()
    overflow_labels: list[str] = []
    stdout_thread = threading.Thread(
        target=_read_pipe,
        kwargs={
            "stream": process.stdout,
            "label": "external sensor stdout",
            "limit": request.max_stdout_bytes,
            "chunks": stdout_chunks,
            "overflow_event": overflow_event,
            "overflow_labels": overflow_labels,
        },
        name="synapse-infigraph-stdout-reader",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_pipe,
        kwargs={
            "stream": process.stderr,
            "label": "external sensor stderr",
            "limit": request.max_stderr_bytes,
            "chunks": stderr_chunks,
            "overflow_event": overflow_event,
            "overflow_labels": overflow_labels,
        },
        name="synapse-infigraph-stderr-reader",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    deadline = time.monotonic() + request.timeout_seconds
    try:
        while process.poll() is None:
            if overflow_event.is_set():
                process.kill()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            time.sleep(min(0.01, remaining))
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        _close_pipe(process.stdout)
        _close_pipe(process.stderr)

    if timed_out:
        raise InfigraphProcessError("external sensor process가 제한 시간 안에 끝나지 않았습니다.")
    if overflow_labels:
        raise InfigraphProcessError(f"{overflow_labels[0]}가 허용된 크기를 초과했습니다.")
    return ExternalProcessResult(
        returncode=process.returncode,
        stdout=b"".join(stdout_chunks),
        stderr=b"".join(stderr_chunks),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class InfigraphProcessAdapter:
    """Explicit one-shot process adapter for the Phase 40 Infigraph JSON shape."""

    runner: ExternalProcessRunner | None = None
    adapter_id: str = INFIGRAPH_PROCESS_ADAPTER_ID
    adapter_version: str = INFIGRAPH_PROCESS_ADAPTER_VERSION

    def __post_init__(self) -> None:
        if self.adapter_id != INFIGRAPH_PROCESS_ADAPTER_ID:
            raise InfigraphProcessError("adapter_id는 infigraph-process여야 합니다.")
        if self.adapter_version != INFIGRAPH_PROCESS_ADAPTER_VERSION:
            raise InfigraphProcessError("지원하지 않는 Infigraph process adapter version입니다.")
        if self.runner is not None and not callable(self.runner):
            raise InfigraphProcessError("runner는 callable이어야 합니다.")

    def execute(self, request: ExternalSensorProcessRequest) -> ExternalSensorImport:
        """Explicitly execute exactly one request and import stdout as evidence."""

        if not isinstance(request, ExternalSensorProcessRequest):
            raise InfigraphProcessError("ExternalSensorProcessRequest가 필요합니다.")
        result = (self.runner or _run_process)(request)
        if not isinstance(result, ExternalProcessResult):
            raise InfigraphProcessError("runner는 ExternalProcessResult를 반환해야 합니다.")
        if len(result.stdout) > request.max_stdout_bytes:
            raise InfigraphProcessError("external sensor stdout가 허용된 크기를 초과했습니다.")
        if len(result.stderr) > request.max_stderr_bytes:
            raise InfigraphProcessError("external sensor stderr가 허용된 크기를 초과했습니다.")
        if result.returncode != 0:
            detail = _decode_stderr(result.stderr)
            suffix = f": {detail}" if detail else ""
            raise InfigraphProcessError(
                f"external sensor process가 비정상 종료되었습니다({result.returncode}){suffix}"
            )
        try:
            stdout = result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InfigraphProcessError("external sensor stdout가 UTF-8이 아닙니다.") from exc
        if not stdout.strip():
            raise InfigraphProcessError("external sensor stdout가 비어 있습니다.")
        try:
            structural_import = InfigraphJsonAdapter().import_observation(stdout)
        except (InfigraphAdapterError, TypeError, ValueError) as exc:
            raise InfigraphProcessError(
                f"external sensor stdout를 Infigraph observation으로 가져오지 못했습니다: {exc}"
            ) from exc
        receipt = ExternalSensorProcessReceipt(
            request_id=request.request_id,
            command=request.command,
            cwd=str(request.cwd) if request.cwd is not None else None,
            timeout_seconds=request.timeout_seconds,
            max_stdout_bytes=request.max_stdout_bytes,
            max_stderr_bytes=request.max_stderr_bytes,
            exit_code=result.returncode,
            stdout_sha256=_sha256(result.stdout),
            stderr_sha256=_sha256(result.stderr),
            stderr_excerpt=_decode_stderr(result.stderr),
            observation_id=structural_import.observation.id,
            observation_hash=structural_import.observation.observation_hash,
            source_id=structural_import.observation.source_id,
            workspace_hash=structural_import.observation.workspace_hash,
        )
        return ExternalSensorImport(
            structural_import=structural_import,
            process_receipt=receipt,
        )


__all__ = [
    "INFIGRAPH_PROCESS_ADAPTER_ID",
    "INFIGRAPH_PROCESS_ADAPTER_VERSION",
    "INFIGRAPH_PROCESS_SCHEMA",
    "MAX_PROCESS_STDERR_BYTES",
    "MAX_PROCESS_STDOUT_BYTES",
    "MAX_PROCESS_TIMEOUT_SECONDS",
    "ExternalProcessResult",
    "ExternalSensorImport",
    "ExternalSensorProcessReceipt",
    "ExternalSensorProcessRequest",
    "InfigraphProcessAdapter",
    "InfigraphProcessError",
]
