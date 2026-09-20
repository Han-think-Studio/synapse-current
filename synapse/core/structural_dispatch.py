"""One owner for the explicit structural review-to-dispatch order.

Phases 51 through 56 each gated one link of a sequence: inspect a materialized
report, project a bounded context from it, bind that context to an executor
route, prepare a provider-neutral request, prepare a provider request, and
dispatch it once. Every link was reachable only from ``self_check`` and tests,
so the sequence existed but nothing could run it.

This module defines that order **once** and gives it two roles: the
deterministic self-check that verifies it, and the CLI below that a person can
run. Neither restates the order. Adding a second copy of the sequence would be
the duplicate injection this project exists to prevent.

Nothing here discovers a report, a provider, a model, or a command; every input
is supplied by the caller. Dry run is the default -- without explicit approval
the sequence stops at the prepared provider request and no transport is invoked.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.core.action import ActionRequest
from synapse.core.structural_context import StructuralContextRequest
from synapse.core.structural_executor_handoff import (
    StructuralExecutorHandoff,
    prepare_structural_executor_handoff,
)
from synapse.core.structural_export_report_inspection import (
    StructuralExportReportInspection,
    inspect_structural_export_report_file,
)
from synapse.core.structural_provider_dispatch import (
    StructuralProviderDispatchResult,
    dispatch_structural_provider_request,
)
from synapse.core.structural_provider_plan import (
    StructuralProviderRequestPlan,
    prepare_structural_provider_request,
)
from synapse.core.structural_report_context import (
    StructuralReportContextResult,
    build_bounded_context_from_verified_report,
)
from synapse.core.structural_runtime_plan import (
    StructuralRuntimeRequestPlan,
    prepare_structural_runtime_request,
)
from synapse.runtime.http_transport import JsonTransport, UrllibJsonTransport
from synapse.runtime.process_transport import AllowlistedProcessTransport
from synapse.runtime.registry import get_builtin_adapter

STRUCTURAL_DISPATCH_SCHEMA = "structural.dispatch.v1"

#: The order itself. This tuple is the single authority for the sequence; the
#: self-check and the CLI both read their expectations from it.
STRUCTURAL_DISPATCH_STAGES = (
    "REPORT_INSPECTED",
    "CONTEXT_PROJECTED",
    "HANDOFF_PREPARED",
    "RUNTIME_REQUEST_PREPARED",
    "PROVIDER_REQUEST_PREPARED",
    "DISPATCHED",
)
STRUCTURAL_DISPATCH_STATUSES = frozenset({"PREPARED", "REVIEW", "SUCCEEDED", "FAILED"})

#: Distinct from the Phase 48 review code (4) and the Phase 51 inspection code (6)
#: so a caller can tell which stage refused.
STRUCTURAL_DISPATCH_ERROR_EXIT_CODE = 7

_DEFAULT_TASK_KIND = "structural_context_review"


class StructuralDispatchError(ValueError):
    """Raised when the explicit structural dispatch sequence fails closed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralDispatchResult:
    """Immutable, non-authoritative record of one explicit run of the sequence."""

    inspection: StructuralExportReportInspection
    context_result: StructuralReportContextResult
    handoff: StructuralExecutorHandoff
    runtime_plan: StructuralRuntimeRequestPlan | None
    provider_plan: StructuralProviderRequestPlan | None
    dispatch: StructuralProviderDispatchResult | None
    status: str
    stages: tuple[str, ...]
    schema_version: str = STRUCTURAL_DISPATCH_SCHEMA
    dispatch_approved: bool = False
    automatic: bool = False
    filesystem_mutation: bool = False
    source_mutation: bool = False
    workspace_mutation: bool = False
    canonical_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != STRUCTURAL_DISPATCH_SCHEMA:
            raise StructuralDispatchError("지원하지 않는 structural dispatch schema입니다.")
        if self.status not in STRUCTURAL_DISPATCH_STATUSES:
            raise StructuralDispatchError("지원하지 않는 structural dispatch 상태입니다.")
        if self.stages != STRUCTURAL_DISPATCH_STAGES[: len(self.stages)]:
            raise StructuralDispatchError("structural dispatch 단계 순서가 계약과 다릅니다.")
        if self.status == "REVIEW" and self.runtime_plan is not None:
            raise StructuralDispatchError("REVIEW 결과는 RuntimeRequest를 만들 수 없습니다.")
        if self.dispatch is None and self.status in {"SUCCEEDED", "FAILED"}:
            raise StructuralDispatchError("dispatch 없이 성공/실패 상태를 가질 수 없습니다.")
        if self.dispatch is not None and not self.dispatch_approved:
            raise StructuralDispatchError("승인 없이 dispatch가 수행될 수 없습니다.")

    @property
    def gate_status(self) -> str:
        """The structural policy gate status the report carried in."""

        return self.inspection.report.review.policy_gate.status

    @property
    def gate_exit_code(self) -> int:
        """Reuse the existing gate exit codes; this module invents none."""

        return self.inspection.gate_exit_code

    @property
    def response_text(self) -> str | None:
        """The model's answer, present only on a successful approved dispatch.

        The serialized receipt never carries it -- this reads the in-memory
        response so an explicit caller can show it once, exactly as the Phase 46
        live path does.
        """

        if self.dispatch is None or self.dispatch.status != "SUCCEEDED":
            return None
        response = self.dispatch.operation.response
        return None if response is None else response.text

    def to_record(self) -> dict[str, Any]:
        """Bounded metadata only; no prompt body and no response body."""

        record: dict[str, Any] = {
            "schema_version": self.schema_version,
            "status": self.status,
            "stages": list(self.stages),
            "dispatch_approved": self.dispatch_approved,
            "gate_status": self.gate_status,
            "gate_exit_code": self.gate_exit_code,
            "report_id": self.inspection.report.id,
            "inspection_id": self.inspection.id,
            "context_id": self.context_result.context.id,
            "context_hash": self.context_result.context.context_hash,
            "handoff_id": self.handoff.id,
            "handoff_status": self.handoff.status,
            "handoff_reason": self.handoff.reason,
            "node_count": len(self.context_result.context.nodes),
            "edge_count": len(self.context_result.context.edges),
            "unresolved_count": len(self.context_result.context.unresolved),
            "automatic": self.automatic,
            "filesystem_mutation": self.filesystem_mutation,
            "source_mutation": self.source_mutation,
            "workspace_mutation": self.workspace_mutation,
            "canonical_mutation": self.canonical_mutation,
        }
        if self.runtime_plan is not None:
            request = self.runtime_plan.runtime_request
            record["runtime_plan_id"] = self.runtime_plan.id
            record["request_id"] = request.request_id
            record["model"] = self.runtime_plan.model
            record["prompt_bytes"] = sum(
                len(message["content"].encode("utf-8")) for message in request.messages
            )
        if self.provider_plan is not None:
            record["provider"] = self.provider_plan.provider
            record["endpoint"] = self.provider_plan.endpoint
            record["provider_plan_id"] = self.provider_plan.id
        if self.dispatch is not None:
            record["dispatch_id"] = self.dispatch.id
            record["dispatch_status"] = self.dispatch.status
            record["response_hash"] = self.dispatch.operation.response_hash
            record["retry_count"] = self.dispatch.retry_count
            record["fallback_used"] = self.dispatch.fallback_used
        return record

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def run_structural_dispatch(
    report: str | Path | StructuralExportReportInspection,
    *,
    context_request: StructuralContextRequest,
    action_request: ActionRequest,
    model: str,
    adapter: object,
    transport: JsonTransport | None = None,
    dispatch_approved: bool = False,
    parameters: Mapping[str, Any] | None = None,
    clock: Any = None,
) -> StructuralDispatchResult:
    """Run the explicit review-to-dispatch order once, in one place.

    Without ``dispatch_approved`` the sequence stops at the prepared provider
    request: no transport is invoked and nothing leaves the process. A report
    whose structural gate is not PASS stops earlier still, at the handoff, as
    REVIEW -- a non-passing gate never becomes dispatch approval.
    """

    if not isinstance(context_request, StructuralContextRequest):
        raise StructuralDispatchError("context_request는 StructuralContextRequest여야 합니다.")
    if not isinstance(action_request, ActionRequest):
        raise StructuralDispatchError("action_request는 ActionRequest여야 합니다.")
    if dispatch_approved and transport is None:
        raise StructuralDispatchError("승인된 dispatch에는 명시적인 transport가 필요합니다.")

    inspection = (
        report
        if isinstance(report, StructuralExportReportInspection)
        else inspect_structural_export_report_file(report)
    )
    context_result = build_bounded_context_from_verified_report(inspection, context_request)
    handoff = prepare_structural_executor_handoff(context_result, action_request)

    common = {
        "inspection": inspection,
        "context_result": context_result,
        "handoff": handoff,
    }

    if handoff.status != "READY":
        # A non-PASS gate, or an unapproved action, is review-only by contract.
        return StructuralDispatchResult(
            **common,
            runtime_plan=None,
            provider_plan=None,
            dispatch=None,
            status="REVIEW",
            stages=STRUCTURAL_DISPATCH_STAGES[:3],
            dispatch_approved=False,
        )

    runtime_plan = prepare_structural_runtime_request(
        handoff,
        model=model,
        parameters=parameters,
    )
    provider_plan = prepare_structural_provider_request(runtime_plan, adapter=adapter)

    if not dispatch_approved:
        return StructuralDispatchResult(
            **common,
            runtime_plan=runtime_plan,
            provider_plan=provider_plan,
            dispatch=None,
            status="PREPARED",
            stages=STRUCTURAL_DISPATCH_STAGES[:5],
            dispatch_approved=False,
        )

    dispatch = dispatch_structural_provider_request(
        provider_plan,
        adapter=adapter,
        transport=transport,
        dispatch_approved=True,
        **({} if clock is None else {"clock": clock}),
    )
    return StructuralDispatchResult(
        **common,
        runtime_plan=runtime_plan,
        provider_plan=provider_plan,
        dispatch=dispatch,
        status=dispatch.status,
        stages=STRUCTURAL_DISPATCH_STAGES,
        dispatch_approved=True,
    )


def _build_context_request(args: argparse.Namespace, source_hash: str) -> StructuralContextRequest:
    return StructuralContextRequest(
        target_id=args.target,
        source_hash=source_hash,
        relations=tuple(args.relation),
        max_depth=args.depth,
        max_nodes=args.max_nodes,
        direction=args.direction,
    )


def _build_action_request(context_id: str) -> ActionRequest:
    """Route approval, which Phase 53 keeps separate from dispatch approval.

    Naming a target on the command line *is* the route approval: the user chose
    this action. It still grants nothing on its own -- a READY route leaves
    dispatch_performed and execution_allowed false, and the network is reached
    only when --approve is also given.
    """

    return ActionRequest(
        id=f"action:structural-dispatch:{context_id}",
        task_kind=_DEFAULT_TASK_KIND,
        required_capabilities=("analysis",),
        privacy="internal",
        approval_required=True,
        approved=True,
        attributes={"context_id": context_id},
    )


def _emit(text: str) -> None:
    """Write UTF-8 bytes to stdout regardless of the console code page.

    These records carry Korean reasons. print encodes through the console
    encoding, which on a cp949 terminal turns the output into bytes that are not
    valid UTF-8 -- so anything piping this CLI into a JSON parser fails on the
    ordinary success path, not just on an edge case.
    """

    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the explicit structural review-to-dispatch sequence against one "
            "already-materialized report. Dry run unless --approve is given."
        )
    )
    parser.add_argument("--report-file", type=Path, required=True)
    parser.add_argument("--target", required=True, help="node id to centre the bounded context on")
    parser.add_argument(
        "--relation",
        action="append",
        required=True,
        help="allowed relation; repeat the flag to allow more than one",
    )
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--max-nodes", type=int, default=100)
    parser.add_argument("--direction", default="both", choices=("both", "inbound", "outbound"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="lmstudio", choices=("lmstudio", "ollama", "claude_code"))
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--approve",
        action="store_true",
        help="dispatch approval: send the prepared request once. Without it the sequence still prepares the request but nothing leaves the process.",
    )
    parser.add_argument(
        "--show-response",
        action="store_true",
        help="print the model's answer on a successful approved dispatch",
    )
    args = parser.parse_args(argv)

    try:
        adapter = get_builtin_adapter(args.provider, base_url=args.base_url)
        if adapter is None:
            raise StructuralDispatchError(f"알 수 없는 provider입니다: {args.provider}")
        # Inspect once and hand the inspection to the sequence, so the report file
        # is read a single time and the context request binds to that exact hash.
        inspection = inspect_structural_export_report_file(args.report_file)
        source_hash = inspection.report.review.observation.observation_hash
        context_request = _build_context_request(args, source_hash)
        result = run_structural_dispatch(
            inspection,
            context_request=context_request,
            action_request=_build_action_request(context_request.target_id),
            model=args.model,
            adapter=adapter,
            transport=(
                AllowlistedProcessTransport() if args.provider == "claude_code" else UrllibJsonTransport()
            ) if args.approve else None,
            dispatch_approved=args.approve,
        )
    except (OSError, TypeError, ValueError) as exc:
        _emit(
            json.dumps(
                {
                    "schema_version": STRUCTURAL_DISPATCH_SCHEMA,
                    "status": "FAILED",
                    "error": str(exc),
                    "report_file": str(args.report_file),
                    "dispatch_approved": bool(args.approve),
                    "filesystem_mutation": False,
                    "source_mutation": False,
                    "workspace_mutation": False,
                    "canonical_mutation": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return STRUCTURAL_DISPATCH_ERROR_EXIT_CODE

    _emit(result.to_json())
    if args.show_response and result.response_text is not None:
        _emit(result.response_text)
    if result.status == "FAILED":
        return STRUCTURAL_DISPATCH_ERROR_EXIT_CODE
    return result.gate_exit_code


__all__ = [
    "STRUCTURAL_DISPATCH_ERROR_EXIT_CODE",
    "STRUCTURAL_DISPATCH_SCHEMA",
    "STRUCTURAL_DISPATCH_STAGES",
    "STRUCTURAL_DISPATCH_STATUSES",
    "StructuralDispatchError",
    "StructuralDispatchResult",
    "main",
    "run_structural_dispatch",
]


if __name__ == "__main__":
    raise SystemExit(main())
