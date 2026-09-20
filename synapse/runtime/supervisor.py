"""Read-only supervisor status projection for the existing overview surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_supervisor_status(
    *,
    runtime_setup: Mapping[str, Any] | None = None,
    doctor: Mapping[str, Any] | None = None,
    active_task: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project known health, usage, blockers, and next action without probing."""

    setup = runtime_setup if isinstance(runtime_setup, Mapping) else {}
    runtimes = setup.get("runtimes") if isinstance(setup.get("runtimes"), list) else []
    providers = []
    usage = []
    for runtime in runtimes:
        if not isinstance(runtime, Mapping):
            continue
        health = runtime.get("health") if isinstance(runtime.get("health"), Mapping) else {}
        state = health.get("state", "UNKNOWN")
        if not isinstance(state, str) or not state:
            state = "UNKNOWN"
        pools = health.get("quota_pools") if isinstance(health.get("quota_pools"), list) else []
        providers.append({"provider": runtime.get("provider", "UNKNOWN"), "status": state, "available": runtime.get("status", "UNKNOWN"), "detail": runtime.get("detail", "")})
        usage.append({"provider": runtime.get("provider", "UNKNOWN"), "pools": pools, "state": state})
    blockers = []
    workspace = setup.get("workspace") if isinstance(setup.get("workspace"), Mapping) else {}
    if setup.get("overall_status") not in {None, "READY"}:
        blockers.append({"kind": "runtime_setup", "status": setup.get("overall_status", "UNKNOWN"), "detail": "runtime setup requires review"})
    if workspace.get("status") not in {None, "READY"}:
        blockers.append({"kind": "workspace", "status": workspace.get("status", "UNKNOWN"), "detail": workspace.get("detail", "")})
    for provider in providers:
        if provider["status"] not in {"READY", "UNKNOWN"}:
            blockers.append({"kind": "provider", "provider": provider["provider"], "status": provider["status"], "detail": provider["detail"]})
    task = dict(active_task) if isinstance(active_task, Mapping) else {"status": "UNKNOWN", "id": None, "next_action": None}
    steps = setup.get("next_steps") if isinstance(setup.get("next_steps"), list) else []
    next_action = steps[0] if steps and isinstance(steps[0], str) else "UNKNOWN"
    return {"providers": providers, "usage": usage, "blockers": blockers, "active_task": task, "next_action": next_action, "doctor": dict(doctor) if isinstance(doctor, Mapping) else {"status": "UNKNOWN"}}


__all__ = ["build_supervisor_status"]
