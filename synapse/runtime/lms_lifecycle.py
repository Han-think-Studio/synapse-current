"""Allowlisted LM Studio lifecycle command bridge.

The command runner is injected so the core never spawns provider processes by
itself. A host may bind this to a bounded subprocess executor.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass

from synapse.runtime.cli import build_lms_load_command, build_lms_unload_command

CommandRunner = Callable[[Sequence[str], float], tuple[int, str]]


def run_with_timeout_cleanup(operation: Callable[[], object], cleanup: Callable[[], bool], *, timeout_seconds: float) -> tuple[str, bool, object | None]:
    """Keep the supervisor alive long enough to run cleanup after timeout."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(operation)
    try:
        return "SUCCEEDED", True, future.result(timeout=timeout_seconds)
    except TimeoutError:
        result = ("TIMEOUT", cleanup(), None)
        pool.shutdown(wait=False, cancel_futures=True)
        return result
    except Exception:  # noqa: BLE001 - isolate provider operation failures
        result = ("FAILED", cleanup(), None)
        pool.shutdown(wait=False, cancel_futures=True)
        return result
    finally:
        if future.done():
            pool.shutdown(wait=True, cancel_futures=True)


def bounded_subprocess_runner(command: Sequence[str], timeout_seconds: float) -> tuple[int, str]:
    """Run one explicitly supplied command and terminate it on timeout."""
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("command must contain non-empty strings")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        completed = subprocess.run(tuple(command), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds, check=False)
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, output[:2000]
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return 124, (output + " command timeout")[:2000]


@dataclass(frozen=True, slots=True)
class LMSLifecycleExecutor:
    run: CommandRunner
    timeout_seconds: float = 30.0

    def _execute(self, command: tuple[str, ...]) -> tuple[bool, str]:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        code, output = self.run(command, self.timeout_seconds)
        return code == 0, str(output)[:2000]

    def load(self, model: str) -> tuple[bool, str]:
        return self._execute(build_lms_load_command(model))

    def unload(self, model: str) -> tuple[bool, str]:
        return self._execute(build_lms_unload_command(model))

    def unload_and_verify(self, model: str, *, poll_seconds: float = 1.0) -> tuple[bool, str]:
        """Unload a model and poll until LM Studio reports no loaded models."""
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        unloaded, unload_output = self.unload(model)
        if not unloaded:
            return False, unload_output
        deadline = time.monotonic() + self.timeout_seconds
        last_output = unload_output
        while time.monotonic() <= deadline:
            status_ok, status_output = self.status()
            last_output = status_output
            if status_ok and "No models are currently loaded" in status_output:
                return True, status_output
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
        return False, f"unload verification timeout: {last_output}"[:2000]

    def status(self) -> tuple[bool, str]:
        return self._execute(("lms", "ps"))

    def callbacks(self, model: str) -> tuple[Callable[[], bool], Callable[[], bool], Callable[[], bool]]:
        """Adapt this executor to lifecycle load/unload/health callbacks."""
        def load() -> bool:
            return self.load(model)[0]
        def unload() -> bool:
            status_ok, status_output = self.status()
            if status_ok and "No models are currently loaded" in status_output:
                return True
            return self.unload_and_verify(model)[0]
        def health() -> bool:
            return self.status()[0]
        return load, unload, health


__all__ = ["CommandRunner", "LMSLifecycleExecutor", "bounded_subprocess_runner", "run_with_timeout_cleanup"]
