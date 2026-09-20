"""User-context broker boundary for Antigravity.

The broker is intentionally narrower than a general process runner.  It owns
the normal-user ``agy`` process context, while Synapse workers see only a
bounded JSON request/response over a local named pipe.  OAuth material is
never read, returned, persisted, or copied by this module.
"""

from __future__ import annotations

import json
import multiprocessing.connection
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.runtime.contracts import HealthEvidence, ProviderHealth, QuotaPool, utc_now

PROVIDER = "antigravity"
ALLOWED_OPERATIONS = frozenset({"antigravity.health", "antigravity.usage", "antigravity.invoke"})
ALLOWED_OUTPUT_FORMATS = frozenset({"json", "text", "stream-json"})
MAX_PROMPT_LENGTH = 32_000
MAX_RESULT_LENGTH = 32_000
MAX_ERROR_LENGTH = 4_000
_ALLOWED_EXECUTABLE_NAMES = frozenset({"agy", "agy.exe", "antigravity", "antigravity.exe"})
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]+$")

# These patterns are deliberately conservative: redaction is defense in depth,
# not a reason to pass secrets into the broker.
_SECRET_PATTERN = re.compile(
    r"(?i)(bearer\s+|access[_ -]?token\s*[:=]\s*|refresh[_ -]?token\s*[:=]\s*|client[_ -]?secret\s*[:=]\s*)[^\s,;]+"
)


class BrokerProtocolError(ValueError):
    """A malformed or unauthorized broker request."""


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    operation: str
    task_id: str | None = None
    prompt: str | None = None
    model: str | None = None
    timeout: float = 60.0
    output_format: str = "json"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BrokerRequest:
        if not isinstance(value, Mapping):
            raise BrokerProtocolError("request must be an object")
        allowed = {"operation", "task_id", "prompt", "model", "timeout", "output_format"}
        unknown = set(value) - allowed
        if unknown:
            raise BrokerProtocolError(f"unknown request fields: {', '.join(sorted(unknown))}")
        operation = value.get("operation")
        if operation not in ALLOWED_OPERATIONS:
            raise BrokerProtocolError("operation is not allowlisted")
        output_format = value.get("output_format", "json")
        if output_format not in ALLOWED_OUTPUT_FORMATS:
            raise BrokerProtocolError("output_format is not supported")
        timeout = value.get("timeout", 60.0)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 600
        ):
            raise BrokerProtocolError("timeout must be between 0 and 600 seconds")
        task_id = value.get("task_id")
        prompt = value.get("prompt")
        model = value.get("model")
        if operation == "antigravity.invoke":
            if not isinstance(task_id, str) or not task_id.strip():
                raise BrokerProtocolError("invoke requires task_id")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_LENGTH:
                raise BrokerProtocolError("invoke requires a bounded prompt")
        for item, label in ((task_id, "task_id"), (model, "model")):
            if item is not None and (not isinstance(item, str) or len(item) > 512):
                raise BrokerProtocolError(f"{label} must be a bounded string")
            if item is not None and not _SAFE_ID_PATTERN.fullmatch(item):
                raise BrokerProtocolError(f"{label} contains unsupported characters")
        return cls(
            operation=operation,
            task_id=task_id,
            prompt=prompt,
            model=model,
            timeout=float(timeout),
            output_format=output_format,
        )

    def to_record(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "operation": self.operation,
                "task_id": self.task_id,
                "prompt": self.prompt,
                "model": self.model,
                "timeout": self.timeout,
                "output_format": self.output_format,
            }.items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class BrokerResult:
    status: str
    exit_code: int | None
    provider_state: str
    quota_state: str
    result: str
    error: str
    duration: float

    def to_record(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "provider_state": self.provider_state,
            "quota_state": self.quota_state,
            "sanitized_stdout": _sanitize(self.result, MAX_RESULT_LENGTH),
            "sanitized_result": _sanitize(self.result, MAX_RESULT_LENGTH),
            "sanitized_error": _sanitize(self.error, MAX_ERROR_LENGTH),
            # The cap above is the one place in this project that certainly cuts
            # a model result. Say so here: a reader downstream cannot tell a
            # trimmed answer from a whole one, and the adapter used to call both
            # a clean stop.
            "result_truncated": _was_truncated(self.result, MAX_RESULT_LENGTH),
            "duration": round(max(0.0, self.duration), 3),
        }


def _redact(value: str | None) -> str:
    return _SECRET_PATTERN.sub(r"\1[REDACTED]", str(value or ""))


def _sanitize(value: str | None, limit: int) -> str:
    return _redact(value)[:limit]


def _was_truncated(value: str | None, limit: int) -> bool:
    """Whether the cap, not the provider, ended this text.

    Measured after redaction because that is what the cap is applied to.
    """

    return len(_redact(value)) > limit


def _state(exit_code: int | None, stdout: str, error: str) -> str:
    combined = f"{stdout}\n{error}".lower()
    if "not logged in" in combined or "oauth" in combined or "authentication" in combined:
        return "AUTH_REQUIRED"
    if exit_code == 0:
        return "READY"
    return "ERROR"


def _quota_state(payload: str) -> str:
    # Only an explicit, bounded provider response can establish quota state.
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return "UNKNOWN"
    if not isinstance(value, Mapping):
        return "UNKNOWN"
    raw = value.get("quota_state")
    return raw if raw in {"READY", "CONSERVE", "RESERVE", "EXHAUSTED", "UNKNOWN"} else "UNKNOWN"


def provider_health(result: BrokerResult, *, checked_at: str | None = None) -> ProviderHealth:
    """Project broker health into the existing provider-health model."""
    observed = checked_at or utc_now()
    quota = QuotaPool(
        provider=PROVIDER,
        account=None,
        pool="antigravity",
        usage_state=result.quota_state,
        evidence=HealthEvidence(
            owner="synapse",
            source="antigravity-broker",
            observed_at=observed,
            reliable=result.quota_state != "UNKNOWN",
        ),
    )
    return ProviderHealth(
        provider=PROVIDER,
        state=result.provider_state,
        installed=True,
        authenticated=result.provider_state == "READY",
        available=result.status == "SUCCEEDED",
        quota_pools=(quota,),
        last_checked=observed,
        error=result.error or None,
        capabilities=("analysis", "code", "external_worker", "noninteractive_print"),
    )


Runner = Callable[[Sequence[str], float], tuple[int, str, str]]


class AntigravityBroker:
    """Allowlisted broker service; no arbitrary command input is accepted."""

    def __init__(self, executable: str = "agy", runner: Runner | None = None) -> None:
        if Path(executable).name.lower() not in _ALLOWED_EXECUTABLE_NAMES:
            raise ValueError("broker executable must be an Antigravity executable")
        self.executable = executable
        self._runner = runner or self._run

    def _run(self, argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
        completed = subprocess.run(
            list(argv),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=None,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def handle(self, request: BrokerRequest | Mapping[str, Any]) -> dict[str, Any]:
        try:
            req = (
                request
                if isinstance(request, BrokerRequest)
                else BrokerRequest.from_mapping(request)
            )
            started = time.monotonic()
            if req.operation == "antigravity.health":
                argv = (
                    self.executable,
                    "-p",
                    "Reply only: __SYNAPSE_HEALTH_OK__",
                    "--output-format",
                    "json",
                )
            elif req.operation == "antigravity.usage":
                argv = (self.executable, "--usage", "--output-format", "json")
            else:
                argv = [
                    self.executable,
                    "-p",
                    req.prompt or "",
                    "--output-format",
                    req.output_format,
                ]
                if req.model:
                    argv[1:1] = ["--model", req.model]
            try:
                code, stdout, stderr = self._runner(tuple(argv), req.timeout)
                state = _state(code, stdout, stderr)
                status = "SUCCEEDED" if code == 0 else "FAILED"
            except subprocess.TimeoutExpired as exc:
                code, stdout, stderr, state, status = (
                    None,
                    str(exc.stdout or ""),
                    str(exc.stderr or ""),
                    "COOLDOWN",
                    "TIMEOUT",
                )
            except OSError as exc:
                code, stdout, stderr, state, status = None, "", str(exc), "NOT_INSTALLED", "FAILED"
            result = BrokerResult(
                status,
                code,
                state,
                _quota_state(stdout),
                stdout,
                stderr,
                time.monotonic() - started,
            )
            record = result.to_record()
            record["provider_health"] = provider_health(result).to_record()
            return record
        except BrokerProtocolError as exc:
            return BrokerResult(
                "REJECTED", None, "UNKNOWN", "UNKNOWN", "", str(exc), 0.0
            ).to_record()

    @staticmethod
    def normalize_creative(*, stage: str, response: Mapping[str, Any] | str,
                           seed_hash: str, global_map: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Pass an existing broker result through the creative boundary."""
        from synapse.core.creative_runtime_bridge import normalize_adapter_creative_response
        return normalize_adapter_creative_response(stage=stage, response=response,
                                                   seed_hash=seed_hash, global_map=global_map)


def serve_named_pipe(
    pipe_name: str, broker: AntigravityBroker, *, authkey: bytes, stop_after: int | None = None
) -> None:
    """Serve newline-independent framed JSON over a Windows named pipe.

    ``authkey`` is an ephemeral IPC capability, not an OAuth credential.  The
    caller must provision it out-of-band for the broker session and never put
    it in repository state.
    """
    if os.name != "nt":
        raise OSError("named-pipe transport requires Windows")
    listener = multiprocessing.connection.Listener(pipe_name, family="AF_PIPE", authkey=authkey)
    handled = 0
    try:
        while stop_after is None or handled < stop_after:
            with listener.accept() as connection:
                request = connection.recv()
                connection.send(broker.handle(request))
                handled += 1
    finally:
        listener.close()


def invoke_named_pipe(
    pipe_name: str, request: Mapping[str, Any], *, authkey: bytes, timeout: float = 30.0
) -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("named-pipe transport requires Windows")
    with multiprocessing.connection.Client(
        pipe_name, family="AF_PIPE", authkey=authkey
    ) as connection:
        connection.send(dict(request))
        if not connection.poll(timeout):
            raise TimeoutError("broker response timed out")
        response = connection.recv()
    if not isinstance(response, dict):
        raise BrokerProtocolError("broker response must be an object")
    return response


__all__ = [
    "ALLOWED_OPERATIONS",
    "AntigravityBroker",
    "BrokerProtocolError",
    "BrokerRequest",
    "BrokerResult",
    "invoke_named_pipe",
    "provider_health",
    "serve_named_pipe",
]
