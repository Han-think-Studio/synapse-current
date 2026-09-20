"""Explicit allowlisted process transport for CLI adapters.

The adapter owns the executable and argv. This transport only executes the
already-prepared request after the caller has passed the existing approval
gate; it never accepts shell text or discovers commands.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping

from synapse.runtime.contracts import PreparedRequest, RuntimeAdapterError, RuntimeErrorKind


class AllowlistedProcessTransport:
    """Run one prepared CLI request with ``shell=False`` and bounded output."""

    def send(self, request: PreparedRequest, *, timeout_seconds: float) -> Mapping[str, object]:
        if request.method != "PROCESS" or not request.url.startswith("process://"):
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "process transport에는 PROCESS 요청만 허용됩니다.")
        command = request.body.get("command")
        args = request.body.get("args")
        if not isinstance(command, str) or not command.strip() or not isinstance(args, list):
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "준비된 process command/args가 올바르지 않습니다.")
        argv = [command, *(item for item in args if isinstance(item, str))]
        if len(argv) != len(args) + 1:
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "process args에는 문자열만 허용됩니다.")
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=min(float(timeout_seconds), float(request.body.get("timeout_seconds", timeout_seconds))),
                check=False,
                cwd=None,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeAdapterError(RuntimeErrorKind.TIMEOUT, f"{request.provider} process timeout") from exc
        except OSError as exc:
            raise RuntimeAdapterError(RuntimeErrorKind.TRANSPORT, f"{request.provider} process failed") from exc
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()[:4000]
        if completed.returncode != 0:
            return {"status": "FAILED", "returncode": completed.returncode, "stderr": stderr}
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {"status": "SUCCEEDED", "text": stdout}
        if not isinstance(payload, Mapping):
            raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, "process 결과가 object가 아닙니다.")
        return dict(payload)


__all__ = ["AllowlistedProcessTransport"]
