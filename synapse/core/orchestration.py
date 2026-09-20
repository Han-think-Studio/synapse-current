"""Caller-owned, single-attempt planner-to-worker orchestration boundary."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from synapse.core.action import ActionRequest, ActionRoute, ActionRouter, RoutingObservation
from synapse.core.packets import ResultPacket, TaskPacket
from synapse.core.run import DurableRun, RunError, RunStatus


class OrchestrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Approval:
    actor: str
    status: str
    task_id: str
    executor_id: str
    attempt: int
    required: bool = True


@dataclass(frozen=True, slots=True)
class OrchestrationReceipt:
    task_id: str
    route: ActionRoute
    result: ResultPacket | None
    run: DurableRun
    status: str
    reason: str
    artifact_id: str | None = None


class OrchestrationCoordinator:
    """Plans routes and invokes exactly the caller-supplied adapter once."""
    def __init__(self, router: ActionRouter | None = None) -> None:
        self.router = router or ActionRouter()

    def plan(self, packet: TaskPacket, *, executor_id: str, capability: str,
             observation: RoutingObservation, approval: Approval | None = None) -> ActionRoute:
        request = ActionRequest(id=f"action:{packet.task_id}", task_kind="delegated_implementation",
                                required_capabilities=(capability,), approval_required=True,
                                approved=False, attributes={"task_id": packet.task_id})
        route = self.router.route(request, observations={executor_id: observation})
        if route.selected is None or route.selected.id != executor_id:
            return route.__class__(request=request, selected=None, candidates=(), status="BLOCKED", reason="requested executor is not admissible")
        if observation.quota_remaining is None or not observation.available or observation.quota_remaining <= 0:
            return route.__class__(request=request, selected=None, candidates=(), status="BLOCKED", reason="runtime health/quota is not admissible")
        return route

    def invoke(self, packet: TaskPacket, run: DurableRun, *, route: ActionRoute,
               approval: Approval, adapter: Callable[[TaskPacket], ResultPacket | Mapping[str, Any]]) -> OrchestrationReceipt:
        rejection = self.validate_invocation(packet, run, route=route, approval=approval)
        if rejection:
            return OrchestrationReceipt(packet.task_id, route, None, run, "REJECTED", rejection)
        try:
            raw_result = adapter(packet)
        except Exception as exc:  # noqa: BLE001 - any adapter failure must be held as an unknown result
            held = self.unknown_result(packet, run, route=route, approval=approval, reason=exc.__class__.__name__)
            return held
        try:
            result = raw_result if isinstance(raw_result, ResultPacket) else ResultPacket.from_record(raw_result)
        except (ValueError, TypeError):
            return self.unknown_result(packet, run, route=route, approval=approval, reason="invalid_result")
        try:
            return self.accept_result(packet, run, route=route, approval=approval, result=result)
        except (OrchestrationError, RunError, ValueError, TypeError) as exc:
            return OrchestrationReceipt(packet.task_id, route, None, run, "REJECTED", str(exc))

    @staticmethod
    def validate_invocation(packet: TaskPacket, run: DurableRun, *, route: ActionRoute,
                            approval: Approval) -> str | None:
        if route.selected is None or route.status == "BLOCKED":
            return route.reason or "route is blocked"
        if approval.required is not True or approval.actor != "Supervisor" or approval.status != "APPROVED":
            return "explicit Supervisor approval is required"
        if (approval.task_id, approval.executor_id) != (packet.task_id, route.selected.id):
            return "approval scope mismatch"
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return "terminal run cannot be invoked again"
        prior = run.checkpoint_for.get(packet.task_id)
        if prior and prior.status is RunStatus.PAUSED_APPROVAL and prior.reason and prior.reason.startswith("RESULT_UNKNOWN"):
            return "prior attempt has an unknown result and requires reconciliation"
        previous = run.checkpoint_for.get(packet.task_id)
        attempt = approval.attempt
        if attempt < 1 or (previous and attempt != previous.attempt + 1) or attempt > run.budget:
            return "attempt exceeds run budget"
        if route.request.attributes.get("task_id") != packet.task_id:
            return "route task scope mismatch"
        return None

    @staticmethod
    def unknown_result(packet: TaskPacket, run: DurableRun, *, route: ActionRoute,
                       approval: Approval, reason: str = "adapter_outcome_unknown",
                       artifact_id: str | None = None) -> OrchestrationReceipt:
        held = run.checkpoint(
            packet.task_id,
            status=RunStatus.PAUSED_APPROVAL,
            artifact_ids=(artifact_id,) if artifact_id else (),
            reason=f"RESULT_UNKNOWN:{reason}",
        )
        return OrchestrationReceipt(packet.task_id, route, None, held, "RESULT_UNKNOWN",
                                    "adapter outcome is unknown; explicit inspection required", artifact_id)

    @staticmethod
    def accept_result(packet: TaskPacket, run: DurableRun, *, route: ActionRoute,
                      approval: Approval, result: ResultPacket,
                      artifact_id: str | None = None) -> OrchestrationReceipt:
        if result.task_id != packet.task_id:
            raise OrchestrationError("result task_id mismatch")
        failed = str(result.status).upper() in {"FAILED", "ERROR"}
        status = RunStatus.PAUSED_RETRYABLE if failed and approval.attempt < 3 else (RunStatus.FAILED if failed else RunStatus.COMPLETED)
        reason = "worker failure" if failed else "result accepted"
        if failed and approval.attempt >= 3:
            result = ResultPacket.from_record({**result.to_record(), "escalation_required": True})
            reason = "three failures: escalation required"
        checkpoint_artifact = artifact_id or f"receipt:{packet.task_id}:{approval.attempt}"
        next_run = run.checkpoint(packet.task_id, status=status,
                                  artifact_ids=(checkpoint_artifact,), reason=reason)
        receipt_id = checkpoint_artifact
        receipt_status = "COMPLETED" if not failed else ("ESCALATION_REQUIRED" if approval.attempt >= 3 else "FAILED")
        return OrchestrationReceipt(packet.task_id, route, result, next_run, receipt_status,
                                    reason, receipt_id)


__all__ = ["Approval", "OrchestrationCoordinator", "OrchestrationError", "OrchestrationReceipt"]
