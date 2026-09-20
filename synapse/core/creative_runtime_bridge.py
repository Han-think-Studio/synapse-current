"""Thin bridge from creative stage requests to the existing runtime boundary."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from synapse.core.action import ActionRouter, ExecutorSpec, RoutingObservation
from synapse.core.creative_contracts import validate_global_map_envelope, validate_zone_envelope
from synapse.core.creative_runtime_request import build_creative_stage_request
from synapse.core.creative_typed_outputs import validate_rich_zone_outputs
from synapse.core.idea_session import canonical_hash
from synapse.core.orchestration import Approval
from synapse.core.orchestration_entry import invoke_entry, plan_entry, route_from_record
from synapse.core.packets import ResultPacket, TaskPacket
from synapse.core.run import create_run
from synapse.core.scaffold import ScaffoldFile, ScaffoldStep, build_scaffold_plan
from synapse.core.structure_map import ARTIFACT_ROLE_PATHS
from synapse.core.workspace import inspect_workspace
from synapse.runtime.contracts import RuntimeResponse, response_was_truncated
from synapse.runtime.operation import run_explicit_runtime_operation


def _provider_object(value: Any) -> Mapping[str, Any]:
    """Decode one provider-shaped response without trusting provider metadata."""

    if isinstance(value, RuntimeResponse):
        value = value.text
    if isinstance(value, Mapping):
        # Preserve provider-side failures as explicit boundary errors.  They
        # must not be fed to an envelope validator (where they would become a
        # misleading schema/auth state) and must never be interpreted as a
        # fresh authentication decision by the creative layer.
        error = value.get("error")
        if error is not None:
            if isinstance(error, Mapping):
                code = str(error.get("code", "provider_error"))
                message = str(error.get("message", error.get("detail", "provider error")))
            else:
                code = str(value.get("code", "provider_error"))
                message = str(error)
            raise ValueError(f"PROVIDER_ERROR: {code}: {message}")
        for key in ("text", "result", "sanitized_result", "sanitized_stdout"):
            if key in value and isinstance(value[key], (Mapping, str)):
                return _provider_object(value[key])
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("MALFORMED_RESPONSE: creative provider response is empty")
    text = value.strip()
    # CLI adapters can return a short diagnostic instead of JSON.  Keep the
    # distinction visible to callers; this is a provider result failure, not
    # evidence that the account needs to be re-authenticated.
    if text.lower().startswith(("authentication required", "auth required", "unauthorized", "forbidden")):
        raise ValueError(f"PROVIDER_ERROR: {text}")
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.insert(0, fenced.group(1))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            return parsed
    raise ValueError("MALFORMED_RESPONSE: expected one JSON object")


def normalize_adapter_creative_response(
    *,
    stage: str,
    response: RuntimeResponse | Mapping[str, Any] | str,
    seed_hash: str,
    global_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a provider adapter or broker result at the creative boundary."""

    return validate_provider_creative_response(
        stage=stage, response=response, seed_hash=seed_hash, global_map=global_map
    )


def validate_provider_creative_response(
    *,
    stage: str,
    response: RuntimeResponse | Mapping[str, Any] | str,
    seed_hash: str,
    global_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize and validate one provider result before creative review.

    This is a pure boundary bridge: it performs no provider call, model
    loading, persistence, routing, or approval.  A zone must be bound to the
    already-validated map supplied by the caller, so malformed or mismatched
    provider output cannot reach quality or synthesis.
    """

    parsed = _provider_object(response)
    if stage == "global_map":
        normalized = validate_global_map_envelope(parsed, require_precontract=True)
        if normalized["source_seed_hash"] != seed_hash:
            raise ValueError("SEED_BINDING_MISMATCH: global map source_seed_hash does not match request seed_hash")
        if normalized["status"] == "VERIFIED":
            raise ValueError("PROVIDER_STATUS_INVALID: global map cannot claim VERIFIED before Synapse review")
        return normalized
    if stage != "zone":
        raise ValueError("unsupported creative provider stage")
    if not isinstance(global_map, Mapping):
        raise ValueError("zone validation requires a validated global_map")  # noqa: TRY004 - malformed wire input is a ValueError contract
    normalized_map = validate_global_map_envelope(global_map)
    # The envelope validator returns immutable tuples.  The zone validator is
    # a wire-boundary validator and intentionally expects JSON arrays here.
    normalized_map = dict(normalized_map)
    normalized_map["zone_plan"] = list(normalized_map["zone_plan"])
    normalized_map["invariants"] = list(normalized_map["invariants"])
    normalized = validate_zone_envelope(parsed, global_map=normalized_map)
    if normalized["status"] == "VERIFIED":
        raise ValueError("PROVIDER_STATUS_INVALID: zone cannot claim VERIFIED before Synapse review")
    normalized["outputs"] = validate_rich_zone_outputs(normalized["zone_type"], normalized["outputs"])
    return normalized


def build_runtime_requester(
    *, model: str, seed_hash: str, provider: str, complete: Callable[[Any], RuntimeResponse],
    dispatch_approved: bool, endpoint: str | None = None, receipts: list[dict[str, Any]] | None = None,
) -> Callable[[str, Mapping[str, Any]], str]:
    """Return a stage requester that reuses the existing one-shot operation.

    This bridge performs no load/unload, retry, persistence, or filesystem work.
    It only translates creative stage payloads and exposes operation receipts.
    """
    validated_global_map: dict[str, Any] | None = None

    def request(stage: str, payload: Mapping[str, Any]) -> str:
        nonlocal validated_global_map
        runtime_request = build_creative_stage_request(stage=stage, model=model, seed_hash=seed_hash, payload=payload)
        result = run_explicit_runtime_operation(
            runtime_request, provider=provider, complete=complete,
            dispatch_approved=dispatch_approved, endpoint=endpoint,
        )
        if receipts is not None:
            receipts.append(result.to_record())
        if result.status != "SUCCEEDED" or result.response is None:
            raise RuntimeError(f"creative runtime operation failed: {result.observation.detail or result.status}")
        if response_was_truncated(result.response):
            # The shape check below would pass a cut-off zone: with a response
            # schema the grammar closes the JSON for the model, so a truncated
            # zone still validates and would be handed on as a whole one.
            raise RuntimeError(
                f"모델 응답이 max_tokens 한도에서 잘렸습니다 "
                f"(stage: {stage}, 원인: {result.response.finish_reason})"
            )
        # Keep the adapter boundary fail-closed.  The runner validates again
        # for direct callers, but a requester returned by this bridge must
        # never leak an unvalidated provider payload to another consumer.
        normalized = validate_provider_creative_response(
            stage=stage,
            response=result.response,
            seed_hash=seed_hash,
            global_map=(
                {**validated_global_map, "zone_plan": list(validated_global_map["zone_plan"]), "invariants": list(validated_global_map["invariants"])}
                if validated_global_map is not None else None
            ),
        )
        if stage == "global_map":
            validated_global_map = normalized
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return request


def _not_invoked_inspection(reason_code: str) -> dict[str, Any]:
    """Describe a preflight refusal without claiming that invocation began."""

    return {
        "invocation": {
            "version": 1,
            "status": "NOT_INVOKED",
            "reason_code": reason_code,
            "artifact_id": None,
            "result": None,
            "run": None,
        },
        "adapter_calls": 0,
        "provider_called": False,
        "provider_quota": "NOT_APPLICABLE",
        "canonical_mutation": False,
        "artifact_mutation": False,
        "domain_quality": "NOT_EVALUATED",
        "literary_quality": "NOT_EVALUATED",
    }


def execute_reviewed_artifact_inspection(
    approval_id: str, *, binding_loader: Callable[[str], Mapping[str, Any]],
    execution_approved: bool, observation: RoutingObservation | None,
    state_path: str | Path, receipt_path: str | Path,
) -> dict[str, Any]:
    """Inspect one host-approved file through the existing durable entry owner.

    This is a host API: the loader, paths, observation and explicit execution
    authorization must come from the application, never arbitrary HTTP fields.
    Content approval alone cannot invoke it. Provider quota is NOT_APPLICABLE
    only for this explicitly offline deterministic adapter.
    """

    if execution_approved is not True:
        return _not_invoked_inspection("EXPLICIT_ACTION_REQUIRED")
    if observation is None:
        return _not_invoked_inspection("AVAILABILITY_OBSERVATION_MISSING")
    if not isinstance(observation, RoutingObservation) or not observation.available:
        return _not_invoked_inspection("EXECUTOR_UNAVAILABLE")
    try:
        binding = json.loads(json.dumps(binding_loader(approval_id)))
    except (KeyError, OSError, TypeError, ValueError):
        return _not_invoked_inspection("BINDING_INVALID")
    identity_keys = (
        "project_id", "workspace_id", "workspace_path", "artifact_role", "artifact_path",
        "file_sha256", "operation", "executor_id", "attempt", "approval_id", "review_id",
        "receipt_hash", "binding_hash",
    )
    try:
        identity = {key: binding[key] for key in identity_keys}
    except (KeyError, TypeError):
        return _not_invoked_inspection("BINDING_INVALID")
    if binding["approval_id"] != approval_id or binding["operation"] != "inspect_artifact":
        return _not_invoked_inspection("APPROVAL_BINDING_MISMATCH")
    if (
        not isinstance(binding["artifact_role"], str)
        or not isinstance(binding["artifact_path"], str)
        or ARTIFACT_ROLE_PATHS.get(binding["artifact_role"]) != binding["artifact_path"]
    ):
        return _not_invoked_inspection("ARTIFACT_SCOPE_MISMATCH")
    if type(binding["attempt"]) is not int or binding["attempt"] != 1:
        return _not_invoked_inspection("ATTEMPT_LIMIT_EXCEEDED")
    try:
        root = Path(binding["workspace_path"]).resolve()
        target = (root / binding["artifact_path"]).resolve()
        target.relative_to(root)
    except (OSError, TypeError, ValueError):
        return _not_invoked_inspection("ARTIFACT_PATH_INVALID")
    # Receipt paths are host-owned and must never overwrite the inspected file.
    try:
        state_target = Path(state_path).resolve()
        receipt_target = Path(receipt_path).resolve()
    except (OSError, TypeError, ValueError):
        return _not_invoked_inspection("RECEIPT_PATH_INVALID")
    if state_target == receipt_target or target in {state_target, receipt_target}:
        return _not_invoked_inspection("RECEIPT_PATH_COLLISION")
    plan = build_scaffold_plan("Inspect the explicitly reviewed artifact", project_name=binding["project_id"])
    plan = replace(
        plan,
        files=(ScaffoldFile(binding["artifact_path"], "Reviewed artifact", "Read-only inspection"),),
        steps=(ScaffoldStep(
            "inspect", "Inspect reviewed artifact", "Read the approved artifact without changes.",
            (binding["artifact_path"],),
        ),),
    )

    def read_current():
        snapshot = inspect_workspace(plan, root)
        file = snapshot.files[0]
        if file.status != "READY" or file.sha256 != str(binding["file_sha256"]).removeprefix("sha256:"):
            raise ValueError("reviewed artifact changed or cannot be safely inspected")
        return file

    try:
        before = read_current()
    except (OSError, ValueError):
        return _not_invoked_inspection("ARTIFACT_INPUT_MISMATCH_OR_UNAVAILABLE")
    packet = TaskPacket.from_record({
        "task_id": "inspect:" + canonical_hash(identity).removeprefix("sha256:"),
        "objective": "Inspect one approved artifact without changing it",
        "scope": identity, "relevant_paths": [str(target)], "known_state": identity,
        "constraints": ["read_only", "one_artifact", "no_provider", "no_automatic_retry"],
        "acceptance_criteria": ["same_input_hash", "bounded_read"], "tests": [],
        "stop_conditions": ["changed_input", "unknown_previous_result"],
        "preferred_capability": "inspect_artifact", "context_budget_hint": 0,
    })
    executor = ExecutorSpec(binding["executor_id"], "python", ("inspect_artifact",), True, 0,
                            network_scope="none", data_egress="prohibited")
    planned = plan_entry(packet, executor_id=executor.id, capability="inspect_artifact",
                         observation=observation, router=ActionRouter((executor,)))
    route = route_from_record(planned["route"])
    approval = Approval("Supervisor", "APPROVED", packet.task_id, executor.id, 1)
    run = create_run(packet.objective, owner="Supervisor", steps=(packet.task_id,), budget=1)
    calls = 0

    def inspect_one(task: TaskPacket) -> ResultPacket:
        nonlocal calls
        calls += 1
        current = binding_loader(approval_id)
        if {key: current[key] for key in identity_keys} != identity:
            raise ValueError("creative review binding changed before inspection")
        file = read_current()
        if file.sha256 != before.sha256:
            raise ValueError("artifact changed during inspection")
        return ResultPacket.from_record({
            "task_id": task.task_id, "status": "COMPLETED", "changed_files": [],
            "commands_executed": [], "test_result": {"bounded_read": True, "same_input_hash": True},
            "relevant_errors": [], "unresolved_issues": ["domain and literary quality not evaluated"],
            "risk": "read_only", "escalation_required": False,
            "short_summary": "Inspected the approved file without modification",
            "artifact": {"path": file.path, "sha256": file.sha256, "size": file.size},
            "content_approval_id": approval_id,
        })

    result = invoke_entry(packet, run, route=route, approval=approval, adapter_id=executor.id,
                          adapters={executor.id: inspect_one}, state_path=state_path, receipt_path=receipt_path)
    return {"invocation": result, "adapter_calls": calls, "provider_called": False,
            "provider_quota": "NOT_APPLICABLE", "canonical_mutation": False,
            "artifact_mutation": False, "domain_quality": "NOT_EVALUATED",
            "literary_quality": "NOT_EVALUATED"}


__all__ = [
    "build_runtime_requester",
    "execute_reviewed_artifact_inspection",
    "normalize_adapter_creative_response",
    "validate_provider_creative_response",
]
