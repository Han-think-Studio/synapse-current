"""Bounded durable run lifecycle with self-validating persistence patterns."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from uuid import uuid4


class RunError(ValueError):
    """Raised when durable run state or a checkpoint is invalid."""


_ARTIFACT_LOCK = threading.RLock()


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED_APPROVAL = "PAUSED_APPROVAL"
    PAUSED_RETRYABLE = "PAUSED_RETRYABLE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    step_id: str
    status: RunStatus
    attempt: int
    artifact_ids: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.step_id.strip() or self.attempt < 1:
            raise RunError("RunCheckpoint step_id/attempt가 잘못되었습니다.")
        object.__setattr__(self, "artifact_ids", tuple(sorted(set(self.artifact_ids))))

    def to_record(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "attempt": self.attempt,
            "artifact_ids": list(self.artifact_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DurableRun:
    id: str
    goal: str
    owner: str
    status: RunStatus
    steps: tuple[str, ...]
    checkpoints: tuple[RunCheckpoint, ...] = ()
    current_step: str | None = None
    revision: int = 0
    budget: int = 1

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.goal.strip() or not self.owner.strip():
            raise RunError("DurableRun id/goal/owner가 필요합니다.")
        if self.revision < 0 or self.budget < 1:
            raise RunError("DurableRun revision/budget가 잘못되었습니다.")
        steps = tuple(dict.fromkeys(step.strip() for step in self.steps if step.strip()))
        if not steps:
            raise RunError("DurableRun에는 하나 이상의 step이 필요합니다.")
        if self.current_step is not None and self.current_step not in steps:
            raise RunError(f"current_step이 steps에 없습니다: {self.current_step}")
        if any(checkpoint.step_id not in steps for checkpoint in self.checkpoints):
            raise RunError("checkpoint step이 steps에 없습니다.")
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "checkpoints", tuple(self.checkpoints))

    @property
    def checkpoint_for(self) -> Mapping[str, RunCheckpoint]:
        return {checkpoint.step_id: checkpoint for checkpoint in self.checkpoints}

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "goal": self.goal,
            "owner": self.owner,
            "status": self.status.value,
            "steps": list(self.steps),
            "checkpoints": [checkpoint.to_record() for checkpoint in self.checkpoints],
            "current_step": self.current_step,
            "revision": self.revision,
            "budget": self.budget,
        }

    def checkpoint(
        self,
        step_id: str,
        *,
        status: RunStatus,
        artifact_ids: Iterable[str] = (),
        reason: str | None = None,
    ) -> DurableRun:
        if self.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            raise RunError(f"종료된 run은 checkpoint할 수 없습니다: {self.status.value}")
        if step_id not in self.steps:
            raise RunError(f"run에 없는 step입니다: {step_id}")
        previous = self.checkpoint_for.get(step_id)
        attempt = (previous.attempt + 1) if previous else 1
        if attempt > self.budget:
            raise RunError(f"step budget을 초과했습니다: {step_id}")
        checkpoint = RunCheckpoint(
            step_id=step_id,
            status=status,
            attempt=attempt,
            artifact_ids=tuple(artifact_ids),
            reason=reason,
        )
        checkpoints = tuple(
            checkpoint if existing.step_id == step_id else existing
            for existing in self.checkpoints
        )
        if previous is None:
            checkpoints = (*checkpoints, checkpoint)
        return replace(
            self,
            status=status,
            checkpoints=checkpoints,
            current_step=step_id,
            revision=self.revision + 1,
        )

    def resume(self, *, owner: str) -> DurableRun:
        if owner.strip() != self.owner:
            raise RunError("run owner가 일치하지 않습니다.")
        if self.status not in {RunStatus.PAUSED_APPROVAL, RunStatus.PAUSED_RETRYABLE, RunStatus.RUNNING}:
            raise RunError(f"현재 상태에서는 resume할 수 없습니다: {self.status.value}")
        return replace(self, status=RunStatus.RUNNING, revision=self.revision + 1)


def create_run(goal: str, *, owner: str, steps: Iterable[str], budget: int = 2) -> DurableRun:
    normalized_steps = tuple(dict.fromkeys(step.strip() for step in steps if step.strip()))
    canonical = json.dumps(
        {"goal": goal.strip(), "owner": owner.strip(), "steps": normalized_steps, "budget": budget},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return DurableRun(
        id=f"run:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        goal=goal,
        owner=owner,
        status=RunStatus.PENDING,
        steps=normalized_steps,
        budget=budget,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def save_run(run: DurableRun, path: str | Path) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": "synapse-durable-run", "version": 1, "run": run.to_record()}
    document = {"payload": payload, "sha256": hashlib.sha256(_canonical(payload)).hexdigest()}
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        # Write, flush and sync the complete replacement before publishing it.
        # This keeps an interrupted write from exposing a truncated destination.
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        # Persist the directory entry as well where the platform supports it.
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                # Replacement is already complete; directory sync is an
                # optional durability enhancement on platforms that reject it.
                pass
            finally:
                os.close(directory_fd)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise RunError(f"run state를 저장할 수 없습니다: {destination}") from exc
    return destination


def load_run(path: str | Path) -> DurableRun:
    source = Path(path).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunError(f"run state를 읽을 수 없습니다: {source}") from exc
    if not isinstance(document, Mapping) or not isinstance(document.get("payload"), Mapping):
        raise RunError("run state integrity envelope가 없습니다.")
    if set(document) != {"payload", "sha256"} or not isinstance(document.get("sha256"), str):
        raise RunError("run state integrity envelope가 잘못되었습니다.")
    payload = document["payload"]
    if document.get("sha256") != hashlib.sha256(_canonical(payload)).hexdigest():
        raise RunError("run state checksum이 일치하지 않습니다.")
    if set(payload) != {"format", "version", "run"}:
        raise RunError("run state payload가 잘못되었습니다.")
    if payload.get("format") != "synapse-durable-run" or payload.get("version") != 1:
        raise RunError("지원하지 않는 run state format/version입니다.")
    raw = payload.get("run")
    if not isinstance(raw, Mapping):
        raise RunError("run state payload가 잘못되었습니다.")
    try:
        raw_checkpoints = raw.get("checkpoints", ())
        if not isinstance(raw_checkpoints, (list, tuple)):
            raise RunError("run state checkpoints가 잘못되었습니다.")
        checkpoints = tuple(
            RunCheckpoint(
                step_id=str(item["step_id"]),
                status=RunStatus(str(item["status"])),
                attempt=int(item["attempt"]),
                artifact_ids=tuple(item.get("artifact_ids", ())),
                reason=item.get("reason"),
            )
            for item in raw_checkpoints
        )
        if len({checkpoint.step_id for checkpoint in checkpoints}) != len(checkpoints):
            raise RunError("run state checkpoint가 중복되었습니다.")
        raw_steps = raw["steps"]
        if not isinstance(raw_steps, (list, tuple)) or any(not isinstance(step, str) for step in raw_steps):
            raise RunError("run state steps가 잘못되었습니다.")
        if raw.get("current_step") is not None and not isinstance(raw.get("current_step"), str):
            raise RunError("run state current_step가 잘못되었습니다.")
        return DurableRun(
            id=str(raw["id"]),
            goal=str(raw["goal"]),
            owner=str(raw["owner"]),
            status=RunStatus(str(raw["status"])),
            steps=tuple(raw_steps),
            checkpoints=checkpoints,
            current_step=raw.get("current_step"),
            revision=int(raw.get("revision", 0)),
            budget=int(raw.get("budget", 1)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RunError("run state 구조가 잘못되었습니다.") from exc


def read_artifacts(path: str | Path) -> list[dict[str, object]]:
    """Read and verify every record in an existing JSONL artifact log."""

    source = Path(path).expanduser().resolve()
    with _ARTIFACT_LOCK:
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise RunError(f"JSONL artifact를 읽을 수 없습니다: {source}") from exc
        artifacts: list[dict[str, object]] = []
        for line_number, line in enumerate(lines, start=1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunError(f"JSONL artifact가 손상되었습니다: {source}:{line_number}") from exc
            if not isinstance(item, Mapping) or set(item) != {"artifact_id", "run_id", "kind", "payload"}:
                raise RunError(f"JSONL artifact 구조가 잘못되었습니다: {source}:{line_number}")
            if not all(isinstance(item.get(key), str) for key in ("artifact_id", "run_id", "kind")):
                raise RunError(f"JSONL artifact identity가 잘못되었습니다: {source}:{line_number}")
            if not isinstance(item.get("payload"), Mapping):
                raise RunError(f"JSONL artifact payload가 잘못되었습니다: {source}:{line_number}")
            record_payload = {"run_id": item["run_id"], "kind": item["kind"], "payload": dict(item["payload"])}
            expected_id = f"artifact:{hashlib.sha256(_canonical(record_payload)).hexdigest()}"
            if item["artifact_id"] != expected_id:
                raise RunError(f"JSONL artifact checksum이 일치하지 않습니다: {source}:{line_number}")
            artifacts.append(dict(item))
        return artifacts


def append_artifact(
    path: str | Path,
    *,
    run_id: str,
    kind: str,
    payload: Mapping[str, object],
    deduplicate: bool = False,
) -> dict[str, object]:
    if not run_id.strip() or not kind.strip():
        raise RunError("artifact run_id/kind가 필요합니다.")
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    record_payload = {"run_id": run_id, "kind": kind, "payload": dict(payload)}
    artifact = {
        "artifact_id": f"artifact:{hashlib.sha256(_canonical(record_payload)).hexdigest()}",
        **record_payload,
    }
    try:
        with _ARTIFACT_LOCK:
            if deduplicate:
                existing = read_artifacts(destination)
                for item in existing:
                    if item["artifact_id"] == artifact["artifact_id"]:
                        return item
            with destination.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        raise RunError(f"JSONL artifact를 저장할 수 없습니다: {destination}") from exc
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or inspect a Synapse durable run")
    parser.add_argument("goal")
    parser.add_argument("--owner", default="human")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--step", action="append", required=True)
    args = parser.parse_args(argv)
    run = create_run(args.goal, owner=args.owner, steps=args.step)
    save_run(run, args.state)
    print(json.dumps(run.to_record(), ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "DurableRun",
    "RunCheckpoint",
    "RunError",
    "RunStatus",
    "append_artifact",
    "create_run",
    "load_run",
    "main",
    "read_artifacts",
    "save_run",
]


if __name__ == "__main__":
    raise SystemExit(main())
