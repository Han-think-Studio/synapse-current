"""Read-only end-to-end reactive control-plane trace."""

from __future__ import annotations

from dataclasses import dataclass

from synapse.core.action import ActionRoute, route_table_action
from synapse.core.bundle import BundleReport
from synapse.core.cards import CardRoute, CardRouter, RouterEvent
from synapse.core.cognitive import CognitiveFrame, build_cognitive_frame
from synapse.core.events import observe_bundle
from synapse.core.failure import FailureReport, triage_frame
from synapse.core.guided import GuidedBuildPlan, build_guided_plan
from synapse.core.intake import bundle_report_to_ir
from synapse.core.ir import SynapseIR
from synapse.core.runtime_plan import RuntimeDispatchPlan, prepare_runtime_plan
from synapse.core.table import CognitiveTable, build_cognitive_table


@dataclass(frozen=True, slots=True)
class ReactiveTrace:
    report: BundleReport
    ir: SynapseIR
    frame: CognitiveFrame
    failures: FailureReport
    plan: GuidedBuildPlan
    event: RouterEvent
    cards: CardRoute
    table: CognitiveTable
    action: ActionRoute
    runtime: RuntimeDispatchPlan

    def to_record(self) -> dict[str, object]:
        return {
            "source_id": self.report.source_id,
            "archive_sha256": self.report.archive_sha256,
            "ir": {"nodes": len(self.ir.nodes), "relations": len(self.ir.relations)},
            "frame_id": self.frame.id,
            "failure_report": self.failures.to_record(),
            "guided_plan": {"id": self.plan.id, "status": self.plan.status},
            "event": self.event.to_record(),
            "cards": self.cards.to_record(),
            "table": {
                "id": self.table.id,
                "selected_ids": list(self.table.selected_ids),
                "unresolved": list(self.table.unresolved),
            },
            "action": self.action.to_record(),
            "runtime": self.runtime.to_record(),
            "network_dispatched": False,
            "canonical_mutation": False,
        }


def run_reactive_trace(
    report: BundleReport,
    card_router: CardRouter,
    *,
    owner: str = "human",
    profile_id: str = "idea_notebook_v1",
) -> ReactiveTrace:
    """Run all read-only control-plane stages for one reviewed bundle."""
    if not report.safe:
        raise ValueError("unsafe bundle은 reactive trace에 사용할 수 없습니다.")
    ir = bundle_report_to_ir(report, owner=owner)
    frame = build_cognitive_frame(ir)
    failures = triage_frame(frame)
    plan = build_guided_plan(ir, frame=frame, failures=failures)
    event = observe_bundle(report)
    cards = card_router.route(event, profile_id=profile_id)
    table = build_cognitive_table(frame, cards)
    action = route_table_action(table.id)
    runtime = prepare_runtime_plan(table, action)
    return ReactiveTrace(
        report=report,
        ir=ir,
        frame=frame,
        failures=failures,
        plan=plan,
        event=event,
        cards=cards,
        table=table,
        action=action,
        runtime=runtime,
    )


__all__ = ["ReactiveTrace", "run_reactive_trace"]
