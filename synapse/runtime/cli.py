"""Provider-neutral boundaries for explicitly run external CLI workers.

This module prepares command contracts only. It never starts a process, logs
in, reads credentials, or claims that a provider call succeeded.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from typing import Any

from synapse.runtime.contracts import (
    PreparedRequest,
    RuntimeAdapterError,
    RuntimeErrorKind,
    RuntimeRequest,
    RuntimeResponse,
)


def _receipt_hash(value: Mapping[str, Any]) -> str:
    """Hash receipt payload without importing ``synapse.core`` (cycle-safe)."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


ANTIGRAVITY_AUTH_CONTEXT_STATES = frozenset(
    {"AUTHENTICATED", "AUTH_CONTEXT_BLOCKED", "AUTH_REQUIRED", "UNKNOWN"}
)


def classify_antigravity_auth_context(
    *,
    interactive_auth_known: bool | None,
    headless_auth_accessible: bool | None,
    credential_store_accessible_from_worker_context: bool | None,
) -> str:
    """Classify Antigravity auth without treating a worker failure as logout.

    This is an evidence classifier only. It does not invoke ``agy`` and never
    reads, copies, or creates credentials. ``AUTH_CONTEXT_BLOCKED`` means the
    interactive session is known to be authenticated while the current worker
    cannot reach that credential context.
    """

    values = (
        interactive_auth_known,
        headless_auth_accessible,
        credential_store_accessible_from_worker_context,
    )
    if any(value is not None and not isinstance(value, bool) for value in values):
        raise ValueError("Antigravity auth observations must be booleans or None")
    if headless_auth_accessible is True:
        return "AUTHENTICATED"
    if (
        interactive_auth_known is True
        and headless_auth_accessible is False
        and credential_store_accessible_from_worker_context is False
    ):
        return "AUTH_CONTEXT_BLOCKED"
    if interactive_auth_known is False and headless_auth_accessible is False:
        return "AUTH_REQUIRED"
    return "UNKNOWN"


class ClaudeCodeAdapter:
    """Prepare a Claude Code ``--print`` invocation for an explicit caller."""

    provider = "claude_code"
    execution_scope = "process"
    network_scope = "remote"
    data_egress = "external_allowed"

    def __init__(self, command: str | None = None) -> None:
        self.command = command or shutil.which("claude") or shutil.which("claude-code")
        if not self.command:
            raise RuntimeAdapterError(RuntimeErrorKind.CONFIGURATION, "Claude Code 명령을 찾을 수 없습니다.")

    def prepare(self, request: RuntimeRequest) -> PreparedRequest:
        prompt = "\n".join(str(message.get("content", "")) for message in request.messages)
        return PreparedRequest(
            request_id=request.request_id,
            provider=self.provider,
            method="PROCESS",
            url="process://claude-code",
            headers={},
            body={
                "command": self.command,
                "args": ["-p", "--model", request.model, prompt],
                "timeout_seconds": request.timeout_seconds,
                "approval_required": True,
            },
        )

    def normalize(self, request: RuntimeRequest, payload: Mapping[str, Any]) -> RuntimeResponse:
        if not isinstance(payload, Mapping):
            raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, "Claude Code 결과가 object가 아닙니다.")
        # A process wrapper may return a structured non-zero exit result. Do
        # not let a stale ``result`` field cross the adapter boundary.
        if payload.get("error") is not None or payload.get("status") in {"FAILED", "ERROR"} or (
            payload.get("returncode") is not None and payload.get("returncode") != 0
        ):
            detail = payload.get("error") or payload.get("stderr") or payload.get("status") or "Claude Code process failed"
            raise RuntimeAdapterError(RuntimeErrorKind.PROVIDER, str(detail)[:4000])
        text = payload.get("text", payload.get("result"))
        if not isinstance(text, str):
            raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, "Claude Code 결과에 text가 없습니다.")
        return RuntimeResponse(
            request_id=request.request_id,
            provider=self.provider,
            model=request.model,
            text=text,
            raw=payload,
        )

    def normalize_creative(self, *, stage: str, response: RuntimeResponse | Mapping[str, Any] | str,
                           seed_hash: str, global_map: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Pass an existing Claude result through the creative boundary."""
        from synapse.core.creative_runtime_bridge import normalize_adapter_creative_response
        return normalize_adapter_creative_response(stage=stage, response=response,
                                                   seed_hash=seed_hash, global_map=global_map)


def build_lms_load_command(model_key: str, *, context_length: int = 8192, parallel: int = 1, ttl_seconds: int = 3600) -> tuple[str, ...]:
    """Build the documented non-interactive LM Studio load command only."""
    if not isinstance(model_key, str) or not model_key.strip():
        raise ValueError("model_key is required")
    if context_length < 1 or parallel < 1 or ttl_seconds < 1:
        raise ValueError("load options must be positive")
    return ("lms", "load", model_key.strip(), "--context-length", str(context_length), "--parallel", str(parallel), "--ttl", str(ttl_seconds), "--yes")


def build_lms_unload_command(identifier: str) -> tuple[str, ...]:
    """Build the documented LM Studio unload command only."""
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("identifier is required")
    return ("lms", "unload", identifier.strip())


def cli_capability_inventory(
    *,
    antigravity_auth_context: Mapping[str, bool | None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return conservative read-only capability facts for external CLIs.

    ``antigravity_auth_context`` is optional caller-supplied probe evidence;
    this function never invokes ``agy`` or touches credential material.
    """

    claude = shutil.which("claude") or shutil.which("claude-code")
    antigravity = {
        "executable_detected": bool(shutil.which("agy") or shutil.which("antigravity")),
        "version_detected": False,
        "machine_readable_output": True,
        "safe_health_probe": "version_help_only",
        "probe_evidence": "documented_help; no task invocation",
        "status": "AVAILABLE" if (shutil.which("agy") or shutil.which("antigravity")) else "UNKNOWN",
        "command": shutil.which("agy") or shutil.which("antigravity"),
        "capabilities": ("analysis", "code", "external_worker", "noninteractive_print") if (shutil.which("agy") or shutil.which("antigravity")) else (),
        "interactive_available": bool(shutil.which("agy") or shutil.which("antigravity")),
        "automation_available": None,
        "automation_capable": bool(shutil.which("agy") or shutil.which("antigravity")),
        "quota_readable": False,
        "task_execution_available": False,
        "authentication": "UNKNOWN",
        "quota": "UNKNOWN",
        "interactive_auth_known": None,
        "headless_auth_accessible": None,
        "credential_store_accessible_from_worker_context": None,
        "auth_context_status": "UNKNOWN",
        "detail": "agy --version/--help succeeded; auth status is not a supported subcommand and no quota/status surface was identified. Runtime task execution was not invoked.",
    }
    if antigravity_auth_context is not None:
        required = {
            "interactive_auth_known",
            "headless_auth_accessible",
            "credential_store_accessible_from_worker_context",
        }
        missing = required - set(antigravity_auth_context)
        if missing:
            raise ValueError(f"Antigravity auth context is missing: {', '.join(sorted(missing))}")
        status = classify_antigravity_auth_context(
            interactive_auth_known=antigravity_auth_context["interactive_auth_known"],
            headless_auth_accessible=antigravity_auth_context["headless_auth_accessible"],
            credential_store_accessible_from_worker_context=antigravity_auth_context["credential_store_accessible_from_worker_context"],
        )
        antigravity.update(
            {
                "interactive_auth_known": antigravity_auth_context["interactive_auth_known"],
                "headless_auth_accessible": antigravity_auth_context["headless_auth_accessible"],
                "credential_store_accessible_from_worker_context": antigravity_auth_context["credential_store_accessible_from_worker_context"],
                "auth_context_status": status,
                "authentication": status,
                "automation_available": status == "AUTHENTICATED",
                "task_execution_available": status == "AUTHENTICATED" and antigravity["automation_capable"],
            }
        )
    return {
        "claude_code": {
            "executable_detected": bool(claude),
            "version_detected": False,
            "machine_readable_output": True,
            "safe_health_probe": "auth_status_or_doctor_help",
            "probe_evidence": "documented_help; no task invocation",
            "status": "AVAILABLE" if claude else "UNKNOWN",
            "command": claude,
            "capabilities": ("analysis", "code", "external_worker", "noninteractive_print") if claude else (),
            "interactive_available": bool(claude),
            "automation_available": bool(claude),
            "quota_readable": False,
            "task_execution_available": bool(claude),
            "authentication": "UNKNOWN",
            "quota": "UNKNOWN",
        },
        "antigravity": antigravity,
    }


def build_cli_capability_receipt(
    inventory: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a read-only, explicit capability matrix from caller evidence.

    This does not probe a process or credential store.  ``None`` remains
    unknown, and capability shape is kept separate from current availability.
    The receipt is suitable for supervisor display and handoff; it grants no
    execution permission.
    """

    source = inventory if inventory is not None else cli_capability_inventory()
    providers: dict[str, dict[str, Any]] = {}
    for name, raw in sorted(source.items()):
        item = dict(raw)
        providers[name] = {
            "executable_detected": item.get("executable_detected", bool(item.get("command"))),
            "version_detected": item.get("version_detected"),
            "status": item.get("status", "UNKNOWN"),
            "interactive_auth_known": item.get("interactive_auth_known"),
            "headless_auth_accessible": item.get("headless_auth_accessible"),
            "credential_store_accessible_from_worker_context": item.get(
                "credential_store_accessible_from_worker_context"
            ),
            "automation_capable": item.get("automation_capable"),
            "automation_available": item.get("automation_available"),
            "quota_readable": item.get("quota_readable"),
            "task_execution_available": item.get("task_execution_available"),
            "machine_readable_output": item.get("machine_readable_output"),
            "safe_health_probe": item.get("safe_health_probe"),
            "probe_evidence": item.get("probe_evidence"),
            "authentication": item.get("authentication", "UNKNOWN"),
            "quota": item.get("quota", "UNKNOWN"),
            "command": item.get("command"),
        }
    receipt: dict[str, Any] = {
        "schema": "synapse.cli-capability-receipt.v1",
        "providers": providers,
        "read_only": True,
        "execution_allowed": False,
        "credential_mutation_allowed": False,
    }
    receipt["receipt_hash"] = _receipt_hash(receipt)
    return receipt


__all__ = [
    "ANTIGRAVITY_AUTH_CONTEXT_STATES",
    "ClaudeCodeAdapter",
    "build_cli_capability_receipt",
    "build_lms_load_command",
    "build_lms_unload_command",
    "classify_antigravity_auth_context",
    "cli_capability_inventory",
]
