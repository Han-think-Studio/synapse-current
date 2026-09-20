"""Thin JSON entry surface for the caller-owned orchestration boundary."""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from synapse.core.action import (
    ActionRequest,
    ActionRoute,
    ActionRouter,
    ExecutorSpec,
    RoutingObservation,
)
from synapse.core.orchestration import Approval, OrchestrationCoordinator
from synapse.core.packets import ResultPacket, TaskPacket
from synapse.core.run import (
    DurableRun,
    RunError,
    RunStatus,
    append_artifact,
    load_run,
    read_artifacts,
    save_run,
)


class EntryError(ValueError):
    pass


_ENTRY_LOCK_GUARD = threading.Lock()
_ENTRY_LOCKS: dict[tuple[str, str], threading.RLock] = {}


def _entry_lock(state_path: str | Path, receipt_path: str | Path) -> threading.RLock:
    key = (str(Path(state_path).expanduser().resolve()), str(Path(receipt_path).expanduser().resolve()))
    with _ENTRY_LOCK_GUARD:
        return _ENTRY_LOCKS.setdefault(key, threading.RLock())


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _run_identity(run: DurableRun) -> dict[str, Any]:
    return {key: getattr(run, key) for key in ("id", "goal", "owner", "steps", "budget")}


def _run_fingerprint(run: DurableRun) -> str:
    return _digest(run.to_record())


def _operation_identity(packet: TaskPacket, run: DurableRun, route: ActionRoute,
                        approval: Approval, adapter_id: str) -> dict[str, Any]:
    return {
        "run_identity_hash": _digest(_run_identity(run)),
        "task_id": packet.task_id,
        "packet_hash": _digest(packet.to_record()),
        "route_hash": _digest(route.to_record()),
        "approval_hash": _digest({
            "actor": approval.actor, "status": approval.status,
            "task_id": approval.task_id, "executor_id": approval.executor_id,
            "attempt": approval.attempt, "required": approval.required,
        }),
        "adapter_id": adapter_id,
        "attempt": approval.attempt,
    }


def _operation_id(run: DurableRun, packet: TaskPacket, approval: Approval) -> str:
    return _digest({"run_id": run.id, "task_id": packet.task_id, "attempt": approval.attempt})


def _journal_events(receipt_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    try:
        artifacts = read_artifacts(receipt_path)
    except RunError as exc:
        raise EntryError(str(exc)) from exc
    return [
        item for item in artifacts
        if item.get("run_id") == run_id and item.get("kind") in {
            "orchestration-intent", "orchestration-outcome", "orchestration-checkpoint"
        }
    ]


def _load_current_run(run: DurableRun, state_path: str | Path) -> DurableRun:
    path = Path(state_path).expanduser().resolve()
    if not path.exists():
        return run
    try:
        stored = load_run(path)
    except RunError as exc:
        raise EntryError(str(exc)) from exc
    if _run_identity(stored) != _run_identity(run):
        raise EntryError("persisted run identity mismatch")
    return stored


def _response(receipt: Any) -> dict[str, Any]:
    return {
        "version": 1, "status": receipt.status, "reason": receipt.reason,
        "artifact_id": receipt.artifact_id,
        "result": receipt.result.to_record() if receipt.result else None,
        "run": receipt.run.to_record(),
    }


def _append_journal(receipt_path: str | Path, run_id: str, kind: str,
                    payload: Mapping[str, object]) -> dict[str, object]:
    try:
        return append_artifact(receipt_path, run_id=run_id, kind=kind,
                               payload=payload, deduplicate=True)
    except RunError as exc:
        raise EntryError(str(exc)) from exc


def _event_for(events: list[dict[str, Any]], kind: str, operation_id: str) -> dict[str, Any] | None:
    matches = [item for item in events if item["kind"] == kind
               and item["payload"].get("operation_id") == operation_id]
    if len(matches) > 1 and any(item != matches[0] for item in matches[1:]):
        raise EntryError(f"conflicting {kind} journal records")
    return matches[0] if matches else None


def _validate_journal_integrity(events: list[dict[str, Any]]) -> None:
    operation_ids: set[str] = set()
    for event in events:
        operation_id = event["payload"].get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise EntryError("orchestration journal operation_id is missing")
        operation_ids.add(operation_id)

    for operation_id in operation_ids:
        intent = _event_for(events, "orchestration-intent", operation_id)
        outcome = _event_for(events, "orchestration-outcome", operation_id)
        checkpoint = _event_for(events, "orchestration-checkpoint", operation_id)
        if intent is None and outcome is not None:
            raise EntryError("orphan orchestration-outcome journal record without intent")
        if intent is None and checkpoint is not None:
            raise EntryError("orphan orchestration-checkpoint journal record without intent")
        if checkpoint is not None and outcome is None:
            raise EntryError("orchestration-checkpoint journal record without outcome")

        positions = {
            kind: [
                index for index, event in enumerate(events)
                if event["kind"] == kind
                and event["payload"].get("operation_id") == operation_id
            ]
            for kind in (
                "orchestration-intent", "orchestration-outcome", "orchestration-checkpoint"
            )
        }
        if (positions["orchestration-intent"] and positions["orchestration-outcome"]
                and max(positions["orchestration-intent"])
                >= min(positions["orchestration-outcome"])):
            raise EntryError("orchestration-outcome journal record precedes intent")
        if (positions["orchestration-outcome"] and positions["orchestration-checkpoint"]
                and max(positions["orchestration-outcome"])
                >= min(positions["orchestration-checkpoint"])):
            raise EntryError("orchestration-checkpoint journal record precedes outcome")


def _checkpoint_matches(run: DurableRun, task_id: str, attempt: int,
                        status: RunStatus, artifact_id: str, reason: str) -> bool:
    checkpoint = run.checkpoint_for.get(task_id)
    return bool(
        checkpoint and checkpoint.attempt == attempt and checkpoint.status is status
        and checkpoint.artifact_ids == (artifact_id,) and checkpoint.reason == reason
        and run.current_step == task_id and run.status is status
    )


def _finish_known(packet: TaskPacket, run: DurableRun, *, route: ActionRoute,
                  approval: Approval, result: ResultPacket, artifact_id: str):
    from synapse.core.orchestration import OrchestrationCoordinator

    return OrchestrationCoordinator.accept_result(
        packet, run, route=route, approval=approval, result=result, artifact_id=artifact_id
    )


def _reconcile_locked(packet: TaskPacket, run: DurableRun, *, route: ActionRoute,
                      approval: Approval, adapter_id: str, state_path: str | Path,
                      receipt_path: str | Path) -> dict[str, Any] | None:
    current = _load_current_run(run, state_path)
    events = _journal_events(receipt_path, run.id)
    _validate_journal_integrity(events)
    operation_id = _operation_id(run, packet, approval)
    identity = _operation_identity(packet, run, route, approval, adapter_id)
    fingerprint = _digest(identity)
    intent = _event_for(events, "orchestration-intent", operation_id)
    if intent is None:
        return None
    intent_payload = intent["payload"]
    if intent_payload.get("identity") != identity or intent_payload.get("fingerprint") != fingerprint:
        raise EntryError("persisted invocation identity mismatch")
    outcome = _event_for(events, "orchestration-outcome", operation_id)
    checkpoint_event = _event_for(events, "orchestration-checkpoint", operation_id)
    if outcome is None:
        outcome = _append_journal(receipt_path, run.id, "orchestration-outcome", {
            "operation_id": operation_id, "fingerprint": fingerprint,
            "outcome": "RESULT_UNKNOWN", "reason": "intent_without_outcome",
        })
    outcome_payload = outcome["payload"]
    if outcome_payload.get("fingerprint") != fingerprint:
        raise EntryError("persisted outcome identity mismatch")
    if outcome_payload.get("outcome") == "RESULT_UNKNOWN":
        reason = "RESULT_UNKNOWN:adapter_outcome_unknown"
        if not _checkpoint_matches(current, packet.task_id, approval.attempt,
                                   RunStatus.PAUSED_APPROVAL, intent["artifact_id"], reason):
            if _run_fingerprint(current) != intent_payload.get("base_run_hash"):
                raise EntryError("unknown outcome cannot be reconciled with current run state")
            from synapse.core.orchestration import OrchestrationCoordinator
            receipt = OrchestrationCoordinator.unknown_result(
                packet, current, route=route, approval=approval,
                artifact_id=intent["artifact_id"],
            )
            try:
                save_run(receipt.run, state_path)
            except RunError as exc:
                raise EntryError(str(exc)) from exc
            current = receipt.run
        else:
            from synapse.core.orchestration import OrchestrationReceipt
            receipt = OrchestrationReceipt(
                packet.task_id, route, None, current, "RESULT_UNKNOWN",
                "adapter outcome is unknown; explicit inspection required", intent["artifact_id"],
            )
    elif outcome_payload.get("outcome") == "KNOWN":
        try:
            result = ResultPacket.from_record(outcome_payload["result"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EntryError("persisted result packet is corrupt") from exc
        if result.task_id != packet.task_id:
            raise EntryError("persisted result task identity mismatch")
        from synapse.core.orchestration import OrchestrationReceipt

        recorded = current.checkpoint_for.get(packet.task_id)
        already_durable = bool(
            recorded is not None
            and recorded.attempt == approval.attempt
            and recorded.artifact_ids == (outcome["artifact_id"],)
            and current.current_step == packet.task_id
            and current.status is recorded.status
        )
        if already_durable:
            # This attempt's checkpoint survived the crash. Recomputing it would ask a
            # terminal run to checkpoint again, so reconcile from what was persisted.
            failed = str(result.status).upper() in {"FAILED", "ERROR"}
            receipt_status = "COMPLETED" if not failed else (
                "ESCALATION_REQUIRED" if approval.attempt >= 3 else "FAILED"
            )
            receipt = OrchestrationReceipt(packet.task_id, route, result, current,
                                           receipt_status, recorded.reason or "",
                                           outcome["artifact_id"])
        else:
            computed = _finish_known(packet, current, route=route, approval=approval,
                                     result=result, artifact_id=outcome["artifact_id"])
            checkpoint = computed.run.checkpoint_for[packet.task_id]
            if not _checkpoint_matches(current, packet.task_id, approval.attempt,
                                       checkpoint.status, outcome["artifact_id"], checkpoint.reason or ""):
                if _run_fingerprint(current) != intent_payload.get("base_run_hash"):
                    raise EntryError("known outcome cannot be reconciled with current run state")
                try:
                    save_run(computed.run, state_path)
                except RunError as exc:
                    raise EntryError(str(exc)) from exc
                current = computed.run
            receipt = OrchestrationReceipt(packet.task_id, route, result, current,
                                           computed.status, computed.reason, outcome["artifact_id"])
    else:
        raise EntryError("persisted outcome kind is unsupported")
    if checkpoint_event is None:
        _append_journal(receipt_path, run.id, "orchestration-checkpoint", {
            "operation_id": operation_id, "fingerprint": fingerprint,
            "run_revision": receipt.run.revision, "run_status": receipt.run.status.value,
            "outcome_artifact_id": outcome["artifact_id"],
        })
    elif (checkpoint_event["payload"].get("fingerprint") != fingerprint
          or checkpoint_event["payload"].get("run_revision") != receipt.run.revision
          or checkpoint_event["payload"].get("run_status") != receipt.run.status.value
          or checkpoint_event["payload"].get("outcome_artifact_id") != outcome["artifact_id"]):
        raise EntryError("persisted checkpoint event mismatch")
    return _response(receipt)


def reconcile_entry(packet: TaskPacket, run: DurableRun, *, route: ActionRoute,
                    approval: Approval, adapter_id: str, state_path: str | Path,
                    receipt_path: str | Path) -> dict[str, Any] | None:
    """Reconcile a prior durable invocation without calling its adapter.

    The JSONL journal and run file are single-process coordinated; this is not
    a cross-process lock or a transaction spanning both files.
    """

    with _entry_lock(state_path, receipt_path):
        return _reconcile_locked(packet, run, route=route, approval=approval,
                                 adapter_id=adapter_id, state_path=state_path,
                                 receipt_path=receipt_path)


def _call_adapter(adapter: Callable[[TaskPacket], ResultPacket | Mapping[str, Any]],
                  packet: TaskPacket) -> ResultPacket | Mapping[str, Any]:
    return adapter(packet)


def _json(value: str, label: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EntryError(f"malformed {label} JSON") from exc
    if not isinstance(parsed, Mapping):
        raise EntryError(f"{label} must be a JSON object")
    return parsed


def _executor(raw: Mapping[str, Any]) -> ExecutorSpec:
    return ExecutorSpec(id=str(raw["id"]), provider=str(raw["provider"]), capabilities=tuple(raw.get("capabilities", ())), local=bool(raw.get("local", False)), priority=int(raw.get("priority", 0)), network_scope=str(raw.get("network_scope", "")), data_egress=str(raw.get("data_egress", "")))


def route_from_record(raw: Mapping[str, Any]) -> ActionRoute:
    request_raw = raw["request"]
    request = ActionRequest(id=str(request_raw["id"]), task_kind=str(request_raw["task_kind"]), required_capabilities=tuple(request_raw.get("required_capabilities", ())), privacy=str(request_raw.get("privacy", "internal")), approval_required=bool(request_raw.get("approval_required", False)), approved=bool(request_raw.get("approved", False)), attributes=request_raw.get("attributes", {}))
    selected = _executor(raw["selected"]) if raw.get("selected") else None
    return ActionRoute(request=request, selected=selected, candidates=tuple(_executor(item) for item in raw.get("candidates", ())), status=str(raw["status"]), reason=str(raw["reason"]), decision=str(raw.get("decision", "selected")))


def plan_entry(packet: TaskPacket, *, executor_id: str, capability: str, observation: RoutingObservation, router: ActionRouter | None = None) -> dict[str, Any]:
    route = OrchestrationCoordinator(router).plan(packet, executor_id=executor_id, capability=capability, observation=observation)
    approval = Approval(actor="Supervisor", status="PENDING", task_id=packet.task_id, executor_id=executor_id, attempt=1)
    return {"version": 1, "packet": packet.to_record(), "route": route.to_record(), "approval": {"actor": approval.actor, "status": approval.status, "task_id": approval.task_id, "executor_id": approval.executor_id, "attempt": approval.attempt, "required": approval.required}, "observation": {"available": observation.available, "quota_remaining": observation.quota_remaining, "cost": observation.cost, "context_fit": observation.context_fit}}


def invoke_entry(packet: TaskPacket, run: DurableRun, *, route: ActionRoute, approval: Approval, adapter_id: str, adapters: Mapping[str, Callable[[TaskPacket], ResultPacket | Mapping[str, Any]]], state_path: str | Path | None = None, receipt_path: str | Path | None = None) -> dict[str, Any]:
    if adapter_id not in adapters:
        raise EntryError("explicit adapter id is not registered by caller")
    if (state_path is None) != (receipt_path is None):
        raise EntryError("durable invocation requires both state_path and receipt_path")
    if run.owner != approval.actor:
        raise EntryError("run owner mismatch")
    if route.selected is None or route.selected.id != adapter_id:
        raise EntryError("adapter identity mismatch")
    coordinator = OrchestrationCoordinator()
    if state_path is None or receipt_path is None:
        receipt = coordinator.invoke(packet, run, route=route, approval=approval,
                                     adapter=adapters[adapter_id])
        return _response(receipt)

    with _entry_lock(state_path, receipt_path):
        current = _load_current_run(run, state_path)
        resolved_state_path = Path(state_path).expanduser().resolve()
        if not resolved_state_path.exists():
            # A journal is the record of side effects. Reject corruption before
            # creating even the initial DurableRun file for this invocation.
            _validate_journal_integrity(_journal_events(receipt_path, current.id))
            try:
                save_run(current, state_path)
            except RunError as exc:
                return {"version": 1, "status": "REJECTED", "reason": str(exc),
                        "run": current.to_record()}
        recovered = _reconcile_locked(
            packet, current, route=route, approval=approval, adapter_id=adapter_id,
            state_path=state_path, receipt_path=receipt_path,
        )
        if recovered is not None:
            return recovered
        rejection = coordinator.validate_invocation(packet, current, route=route,
                                                    approval=approval)
        if rejection:
            return {"version": 1, "status": "REJECTED", "reason": rejection,
                    "run": current.to_record()}
        history = _journal_events(receipt_path, current.id)
        operation_id = _operation_id(current, packet, approval)
        identity = _operation_identity(packet, current, route, approval, adapter_id)
        fingerprint = _digest(identity)
        for item in history:
            payload = item["payload"]
            if item["kind"] != "orchestration-intent" or payload.get("task_id") != packet.task_id:
                continue
            prior_id = payload.get("operation_id")
            prior_outcome = _event_for(history, "orchestration-outcome", str(prior_id))
            if prior_outcome is None or prior_outcome["payload"].get("outcome") == "RESULT_UNKNOWN":
                return {"version": 1, "status": "RESULT_UNKNOWN",
                        "reason": "prior attempt has unresolved outcome; reconcile it before another attempt",
                        "run": current.to_record()}
            if payload.get("identity", {}).get("packet_hash") != identity["packet_hash"]:
                raise EntryError("task input changed across attempts")
        conflict = _event_for(history, "orchestration-intent", operation_id)
        if conflict is not None:
            raise EntryError("invocation intent exists but could not be reconciled")
        _append_journal(receipt_path, current.id, "orchestration-intent", {
            "operation_id": operation_id, "fingerprint": fingerprint,
            "identity": identity, "task_id": packet.task_id,
            "base_run_hash": _run_fingerprint(current),
        })
        try:
            raw_result = _call_adapter(adapters[adapter_id], packet)
            result = raw_result if isinstance(raw_result, ResultPacket) else ResultPacket.from_record(raw_result)
            if result.task_id != packet.task_id:
                raise EntryError("result task_id mismatch")
            provisional = coordinator.accept_result(packet, current, route=route,
                                                    approval=approval, result=result)
            outcome_payload: dict[str, object] = {
                "operation_id": operation_id, "fingerprint": fingerprint,
                "outcome": "KNOWN", "result": provisional.result.to_record(),
            }
        except Exception as exc:  # noqa: BLE001 - any adapter failure must record an unknown outcome
            outcome_payload = {
                "operation_id": operation_id, "fingerprint": fingerprint,
                "outcome": "RESULT_UNKNOWN", "reason": exc.__class__.__name__,
            }
        _append_journal(receipt_path, current.id, "orchestration-outcome",
                        outcome_payload)
        return _reconcile_locked(
            packet, current, route=route, approval=approval, adapter_id=adapter_id,
            state_path=state_path, receipt_path=receipt_path,
        ) or {"version": 1, "status": "REJECTED", "reason": "outcome reconciliation failed",
              "run": current.to_record()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervisor-controlled Synapse orchestration entry")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan"); plan.add_argument("--packet", required=True); plan.add_argument("--observation", required=True); plan.add_argument("--executor", required=True); plan.add_argument("--capability", required=True)
    invoke = sub.add_parser("invoke"); invoke.add_argument("--packet", required=True); invoke.add_argument("--route", required=True); invoke.add_argument("--approval", required=True); invoke.add_argument("--run-state", required=True); invoke.add_argument("--adapter-id", required=True); invoke.add_argument("--receipt", required=False)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            packet = TaskPacket.from_record(_json(args.packet, "packet")); obs = _json(args.observation, "observation")
            output = plan_entry(packet, executor_id=args.executor, capability=args.capability, observation=RoutingObservation(**dict(obs)))
        else:
            packet = TaskPacket.from_record(_json(args.packet, "packet")); route_from_record(_json(args.route, "route")); approval_raw = _json(args.approval, "approval")
            Approval(actor=str(approval_raw["actor"]), status=str(approval_raw["status"]), task_id=str(approval_raw["task_id"]), executor_id=str(approval_raw["executor_id"]), attempt=int(approval_raw["attempt"]), required=bool(approval_raw.get("required", True)))
            load_run(args.run_state)
            raise EntryError("CLI invoke requires a caller-owned adapter registry; use invoke_entry API")
        print(json.dumps(output, ensure_ascii=False, sort_keys=True)); return 0
    except (KeyError, TypeError, ValueError, RunError, EntryError) as exc:
        print(json.dumps({"version": 1, "status": "REJECTED", "reason": str(exc)}, ensure_ascii=False, sort_keys=True)); return 2


__all__ = ["EntryError", "invoke_entry", "main", "plan_entry", "reconcile_entry", "route_from_record"]

if __name__ == "__main__":
    raise SystemExit(main())
