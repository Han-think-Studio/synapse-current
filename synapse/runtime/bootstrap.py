"""Explicit first-run workspace and local-runtime handshake checks.

The handshake borrows the useful part of durable worker runtimes: discover the
workspace and worker capabilities before attempting a task.  It performs no
model inference and never changes Canonical State.  Network probes are opt-in
and are only run by an explicit caller (for example, the Console button).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synapse.runtime.contracts import (
    HealthEvidence,
    ProviderHealth,
    QuotaPool,
    RuntimeAdapterError,
    RuntimeErrorKind,
)
from synapse.runtime.http_transport import _http_error_detail
from synapse.runtime.policy import validate_endpoint_policy


class BootstrapError(ValueError):
    """Raised when a handshake request is invalid."""


@dataclass(frozen=True, slots=True)
class WorkspaceHandshake:
    """Read-only facts about the local folder that will receive a bundle."""

    path: str
    status: str
    exists: bool
    is_directory: bool
    writable: bool
    manifest_present: bool
    detail: str

    def __post_init__(self) -> None:
        if self.status not in {"NOT_CONFIGURED", "READY", "MISSING", "INVALID"}:
            raise BootstrapError(f"지원하지 않는 workspace 상태입니다: {self.status}")

    def to_record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "status": self.status,
            "exists": self.exists,
            "is_directory": self.is_directory,
            "writable": self.writable,
            "manifest_present": self.manifest_present,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    """A local provider health result; it contains no credentials or model output."""

    id: str
    provider: str
    endpoint: str
    status: str
    models: tuple[str, ...]
    capabilities: tuple[str, ...]
    network_checked: bool
    error_kind: RuntimeErrorKind | None
    detail: str
    health: ProviderHealth | None = None
    context_size: int | None = None
    hardware: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"NOT_CHECKED", "AVAILABLE", "UNAVAILABLE", "MALFORMED"}:
            raise BootstrapError(f"지원하지 않는 runtime 상태입니다: {self.status}")
        object.__setattr__(self, "models", tuple(sorted(set(self.models))))
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        object.__setattr__(self, "hardware", tuple(sorted({item.strip() for item in self.hardware if isinstance(item, str) and item.strip()})))

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "status": self.status,
            "models": list(self.models),
            "capabilities": list(self.capabilities),
            "network_checked": self.network_checked,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "detail": self.detail,
            "health": (self.health or ProviderHealth(provider=self.provider)).to_record(),
            "context_size": self.context_size,
            "hardware": list(self.hardware),
        }


@dataclass(frozen=True, slots=True)
class ExecutorDescriptor:
    """One selectable local executor in the first-run control plane."""

    id: str
    provider: str
    kind: str
    status: str
    local: bool
    endpoint: str | None = None
    command: str | None = None
    models: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    detail: str = ""
    execution_scope: str = ""
    network_scope: str = ""
    data_egress: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.provider.strip():
            raise BootstrapError("ExecutorDescriptor id/provider가 필요합니다.")
        if self.kind not in {"builtin", "service", "cli"}:
            raise BootstrapError(f"지원하지 않는 executor 종류입니다: {self.kind}")
        if self.status not in {"NOT_CHECKED", "AVAILABLE", "UNAVAILABLE", "MALFORMED"}:
            raise BootstrapError(f"지원하지 않는 executor 상태입니다: {self.status}")
        object.__setattr__(self, "models", tuple(sorted(set(self.models))))
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        if not self.execution_scope:
            object.__setattr__(self, "execution_scope", "service" if self.kind == "service" else self.kind)
        if not self.network_scope:
            object.__setattr__(self, "network_scope", "loopback" if self.local else "remote")
        if not self.data_egress:
            object.__setattr__(self, "data_egress", "local_only" if self.local else "external_allowed")

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status,
            "local": self.local,
            "endpoint": self.endpoint,
            "command": self.command,
            "models": list(self.models),
            "capabilities": list(self.capabilities),
            "detail": self.detail,
            "execution_scope": self.execution_scope,
            "network_scope": self.network_scope,
            "data_egress": self.data_egress,
        }


@dataclass(frozen=True, slots=True)
class ConnectionReport:
    """The first-run handshake report used by the CLI and local Console."""

    workspace: WorkspaceHandshake
    runtimes: tuple[RuntimeProbe, ...]
    overall_status: str
    next_steps: tuple[str, ...]
    network_probed: bool
    dispatch_allowed: bool = False
    executors: tuple[ExecutorDescriptor, ...] = ()

    def __post_init__(self) -> None:
        if self.overall_status not in {"SETUP_REQUIRED", "CHECK_REQUIRED", "READY", "RUNTIME_REQUIRED"}:
            raise BootstrapError(f"지원하지 않는 handshake 상태입니다: {self.overall_status}")
        if self.dispatch_allowed:
            raise BootstrapError("handshake report는 dispatch를 허용할 수 없습니다.")

    def to_record(self) -> dict[str, object]:
        return {
            "workspace": self.workspace.to_record(),
            "runtimes": [runtime.to_record() for runtime in self.runtimes],
            "overall_status": self.overall_status,
            "next_steps": list(self.next_steps),
            "network_probed": self.network_probed,
            "dispatch_allowed": self.dispatch_allowed,
            "executors": [executor.to_record() for executor in self.executors],
        }


def handshake_workspace(path: str | Path | None) -> WorkspaceHandshake:
    """Inspect a workspace path without creating, extracting, or writing files."""

    raw = str(path or "").strip()
    if not raw:
        return WorkspaceHandshake(
            path="",
            status="NOT_CONFIGURED",
            exists=False,
            is_directory=False,
            writable=False,
            manifest_present=False,
            detail="workspace 경로가 아직 지정되지 않았습니다.",
        )
    try:
        resolved = Path(raw).expanduser().resolve(strict=False)
        exists = resolved.exists()
        is_directory = resolved.is_dir()
        writable = is_directory and os.access(resolved, os.W_OK)
        manifest_present = (resolved / "synapse.bundle.json").is_file() if is_directory else False
    except (OSError, RuntimeError) as exc:
        return WorkspaceHandshake(
            path=raw,
            status="INVALID",
            exists=False,
            is_directory=False,
            writable=False,
            manifest_present=False,
            detail=f"workspace 경로를 읽지 못했습니다: {exc}",
        )
    if not exists:
        status = "MISSING"
        detail = "workspace 폴더가 아직 없습니다. Stage 대상은 새 폴더여야 합니다."
    elif not is_directory:
        status = "INVALID"
        detail = "workspace 경로가 폴더가 아닙니다."
    elif not writable:
        status = "INVALID"
        detail = "workspace 폴더에 쓸 수 없습니다."
    else:
        status = "READY"
        detail = "workspace 폴더를 사용할 수 있습니다."
    return WorkspaceHandshake(
        path=str(resolved),
        status=status,
        exists=exists,
        is_directory=is_directory,
        writable=writable,
        manifest_present=manifest_present,
        detail=detail,
    )


def _get_json(url: str, *, timeout_seconds: float) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        kind = {
            401: RuntimeErrorKind.AUTHENTICATION,
            403: RuntimeErrorKind.AUTHENTICATION,
            408: RuntimeErrorKind.TIMEOUT,
            429: RuntimeErrorKind.RATE_LIMIT,
            504: RuntimeErrorKind.TIMEOUT,
        }.get(exc.code, RuntimeErrorKind.PROVIDER)
        detail = _http_error_detail(exc)
        suffix = f": {detail}" if detail else ""
        raise RuntimeAdapterError(kind, f"runtime HTTP {exc.code}{suffix}") from exc
    except TimeoutError as exc:
        raise RuntimeAdapterError(RuntimeErrorKind.TIMEOUT, "runtime 연결 시간이 초과되었습니다.") from exc
    except urllib.error.URLError as exc:
        kind = RuntimeErrorKind.TIMEOUT if isinstance(exc.reason, TimeoutError) else RuntimeErrorKind.TRANSPORT
        label = "연결 시간이 초과되었습니다." if kind is RuntimeErrorKind.TIMEOUT else f"연결 실패: {exc.reason}"
        raise RuntimeAdapterError(kind, f"runtime {label}") from exc
    except OSError as exc:
        raise RuntimeAdapterError(RuntimeErrorKind.TRANSPORT, "runtime 연결이 중단되었습니다.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, "runtime 응답이 JSON이 아닙니다.") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, "runtime 응답이 JSON object가 아닙니다.")
    return payload


_NON_TEXT_MODEL_MARKERS = (
    "embedding",
    "embed",
    "text-to-speech",
    "tts",
    "talker",
    "tokenizer",
    "speech",
    "voice",
)


def is_text_generation_model(name: str) -> bool:
    """Whether a model name is a Guided text-generation model.

    An embedding / TTS / speech / voice / tokenizer model is never a valid Guided
    text model; discovery and every direct Guided dispatch use this to keep those
    families out of the text-generation contract. Ported from the canonical-main
    model filter (Phase 81).
    """

    lowered = name.casefold().replace("_", "-")
    return not any(marker in lowered for marker in _NON_TEXT_MODEL_MARKERS)


def _model_names(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw_models = payload.get(key)
    if not isinstance(raw_models, list):
        raise RuntimeAdapterError(RuntimeErrorKind.MALFORMED_RESPONSE, f"runtime 응답에 {key} 목록이 없습니다.")
    names: list[str] = []
    for item in raw_models:
        if isinstance(item, Mapping):
            name = item.get("name") or item.get("id")
            if isinstance(name, str) and name.strip() and is_text_generation_model(name.strip()):
                names.append(name.strip())
    return tuple(names)


def _not_checked(
    *, id: str, provider: str, endpoint: str, capabilities: tuple[str, ...], detail: str
) -> RuntimeProbe:
    return RuntimeProbe(
        id=id,
        provider=provider,
        endpoint=endpoint,
        status="NOT_CHECKED",
        models=(),
        capabilities=capabilities,
        network_checked=False,
        error_kind=None,
        detail=detail,
    )


def _probe(
    *,
    id: str,
    provider: str,
    endpoint: str,
    model_key: str,
    capabilities: tuple[str, ...],
    timeout_seconds: float,
    model_reader: Callable[[Mapping[str, Any], str], tuple[str, ...]] = _model_names,
) -> RuntimeProbe:
    try:
        payload = _get_json(endpoint, timeout_seconds=timeout_seconds)
        models = model_reader(payload, model_key)
    except RuntimeAdapterError as exc:
        status = "MALFORMED" if exc.kind is RuntimeErrorKind.MALFORMED_RESPONSE else "UNAVAILABLE"
        return RuntimeProbe(
            id=id,
            provider=provider,
            endpoint=endpoint,
            status=status,
            models=(),
            capabilities=capabilities,
            network_checked=True,
            error_kind=exc.kind,
            detail=str(exc),
        )
    context_size = payload.get("context_size", payload.get("context_length"))
    if isinstance(context_size, bool) or not isinstance(context_size, int) or context_size <= 0:
        context_size = None
    raw_hardware = payload.get("hardware", payload.get("devices", ()))
    if isinstance(raw_hardware, str):
        hardware = (raw_hardware,)
    elif isinstance(raw_hardware, (list, tuple)):
        hardware = tuple(str(item) for item in raw_hardware if isinstance(item, str) and item.strip())
    else:
        hardware = ()
    raw_capabilities = payload.get("capabilities", ())
    discovered_capabilities = tuple(
        sorted({item.strip() for item in raw_capabilities if isinstance(item, str) and item.strip()})
    ) if isinstance(raw_capabilities, (list, tuple)) else ()
    return RuntimeProbe(
        id=id,
        provider=provider,
        endpoint=endpoint,
        status="AVAILABLE",
        models=models,
        capabilities=tuple(sorted(set(capabilities) | set(discovered_capabilities))),
        network_checked=True,
        error_kind=None,
        detail=f"{provider} service가 응답했습니다.",
        context_size=context_size,
        hardware=hardware,
    )


def _find_command(candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _build_executor_descriptors(
    runtimes: tuple[RuntimeProbe, ...],
) -> tuple[ExecutorDescriptor, ...]:
    by_provider = {runtime.provider: runtime for runtime in runtimes}
    descriptors: list[ExecutorDescriptor] = [
        ExecutorDescriptor(
            id="python",
            provider="python",
            kind="builtin",
            status="AVAILABLE",
            local=True,
            command="python",
            capabilities=("deterministic", "parse", "verify", "scaffold"),
            detail="Synapse 내부 규칙·검사 실행자입니다.",
        )
    ]
    for provider in ("lmstudio", "ollama"):
        runtime = by_provider[provider]
        descriptors.append(
            ExecutorDescriptor(
                id=runtime.id,
                provider=provider,
                kind="service",
                status=runtime.status,
                local=True,
                endpoint=runtime.endpoint,
                models=runtime.models,
                capabilities=runtime.capabilities,
                detail=runtime.detail,
            )
        )
    cli_specs = (
        ("codex", ("codex",), ("analysis", "code", "external_worker")),
        ("claude_code", ("claude", "claude-code"), ("analysis", "code", "external_worker")),
    )
    for executor_id, candidates, capabilities in cli_specs:
        command = _find_command(candidates)
        descriptors.append(
            ExecutorDescriptor(
                id=executor_id,
                provider=executor_id,
                kind="cli",
                status="AVAILABLE" if command else "UNAVAILABLE",
                local=False,
                command=command,
                capabilities=capabilities,
                detail=(
                    f"{executor_id} 명령을 찾았습니다. 실행은 사용자 승인 후 진행합니다."
                    if command
                    else f"{executor_id} 명령이 PATH에서 발견되지 않았습니다."
                ),
            )
        )
    return tuple(descriptors)


def build_connection_report(
    workspace_path: str | Path | None = None,
    *,
    probe: bool = False,
    timeout_seconds: float = 0.8,
    provider_metadata: Mapping[str, Any] | None = None,
    provider_endpoints: Mapping[str, str] | None = None,
) -> ConnectionReport:
    """Build a handshake report; network probes happen only when ``probe=True``."""

    if timeout_seconds <= 0:
        raise BootstrapError("timeout_seconds는 0보다 커야 합니다.")
    workspace = handshake_workspace(workspace_path)
    runtime_specs = (
        {
            "id": "lmstudio",
            "provider": "lmstudio",
            "endpoint": "http://127.0.0.1:1234/v1/models",
            "model_key": "data",
            "capabilities": ("analysis", "llm", "local_model"),
        },
        {
            "id": "ollama",
            "provider": "ollama",
            "endpoint": "http://127.0.0.1:11434/api/tags",
            "model_key": "models",
            "capabilities": ("analysis", "llm", "local_model"),
        },
    )
    if provider_endpoints is not None and not isinstance(provider_endpoints, Mapping):
        raise BootstrapError("provider_endpoints must be an object")
    if provider_endpoints:
        specs = []
        for spec in runtime_specs:
            endpoint = provider_endpoints.get(spec["provider"], spec["endpoint"])
            if not isinstance(endpoint, str) or not endpoint.strip():
                raise BootstrapError("provider endpoint must be a non-empty string")
            try:
                validate_endpoint_policy(endpoint.strip(), network_scope="loopback", data_egress="local_only")
            except ValueError as exc:
                raise BootstrapError(f"invalid local provider endpoint: {exc}") from exc
            clean_endpoint = endpoint.strip().rstrip("/")
            suffix = "/v1/models" if spec["provider"] == "lmstudio" else "/api/tags"
            if not clean_endpoint.endswith(("/v1/models", "/api/tags")):
                clean_endpoint += suffix
            specs.append({**spec, "endpoint": clean_endpoint})
        runtime_specs = tuple(specs)
    if provider_metadata is not None and not isinstance(provider_metadata, Mapping):
        raise BootstrapError("provider_metadata must be an object")
    metadata = provider_metadata or {}
    runtimes = tuple(
        (
            _probe(timeout_seconds=timeout_seconds, **spec)
            if probe
            else _not_checked(
                id=spec["id"],
                provider=spec["provider"],
                endpoint=spec["endpoint"],
                capabilities=spec["capabilities"],
                detail="명시적인 연결 점검 전입니다.",
            )
        )
        for spec in runtime_specs
    )
    def health_for(runtime: RuntimeProbe) -> ProviderHealth:
        raw = metadata.get(runtime.provider)
        if not isinstance(raw, Mapping):
            return ProviderHealth(provider=runtime.provider, available=runtime.status == "AVAILABLE", models=runtime.models)
        if "provider" in raw and raw["provider"] != runtime.provider:
            raise BootstrapError("provider metadata provider mismatch")
        pools = []
        raw_pools = raw.get("quota_pools")
        if raw_pools is not None and not isinstance(raw_pools, list):
            raise BootstrapError("provider metadata quota_pools must be a list")
        def normalize_item(item, *, inherited_pool=None):
            if not isinstance(item, Mapping):
                raise BootstrapError("provider metadata quota pool must be an object")
            if str(item.get("provider", runtime.provider)) != runtime.provider:
                raise BootstrapError("provider metadata quota pool provider mismatch")
            evidence = item.get("evidence")
            ev = HealthEvidence(**evidence) if isinstance(evidence, Mapping) else None
            pool_name = str(item.get("pool", inherited_pool or ""))
            rem = item.get("remaining_percent")
            fraction = item.get("remaining_fraction")
            used = item.get("used_percent", item.get("used_fraction"))
            if rem is None and fraction is None and used is not None:
                fraction = (1 - used) if "used_fraction" in item else None
                if fraction is None:
                    rem = 100 - used
            if rem is None and fraction is not None:
                rem = fraction * 100
            if rem is not None and fraction is not None and abs(float(rem) - float(fraction) * 100) > 1e-9:
                raise BootstrapError("provider metadata quota values are contradictory")
            reset_at = item.get("reset_at")
            pools.append(QuotaPool(provider=str(item.get("provider", runtime.provider)), account=item.get("account"), pool=pool_name, models=tuple(item.get("models", ())), remaining_percent=rem, remaining_fraction=(float(rem) / 100 if rem is not None else fraction), usage_state=item.get("usage_state"), reset_at=reset_at, window=item.get("window"), evidence=ev))
            for child in item.get("windows", ()) or ():
                normalize_item(child, inherited_pool=pool_name)
        for item in raw_pools or ():
            normalize_item(item)
        state = raw.get("state", "UNKNOWN")
        if not isinstance(state, str):
            raise BootstrapError("provider metadata state must be a string")
        raw_evidence = raw.get("evidence")
        top_evidence = HealthEvidence(**raw_evidence) if isinstance(raw_evidence, Mapping) else None
        now = datetime.now(UTC)
        def reliable_now(evidence: HealthEvidence | None) -> bool:
            if evidence is None or not evidence.reliable:
                return False
            observed = datetime.fromisoformat(evidence.observed_at)
            expires = datetime.fromisoformat(evidence.expires_at) if evidence.expires_at else None
            # An unbounded observation is still a snapshot: limit its usable
            # freshness so old caller supplied quota cannot imply readiness.
            return observed <= now and (expires is None or now < expires) and (expires is not None or (now - observed).total_seconds() <= 86400)

        if top_evidence is not None and not reliable_now(top_evidence):
            state = "UNKNOWN"
        if any(p.usage_state is not None and not reliable_now(p.evidence) for p in pools):
            state = "UNKNOWN"
        if pools:
            known = [p.remaining_percent for p in pools if p.remaining_percent is not None and reliable_now(p.evidence) and (p.reset_at is None or datetime.fromisoformat(p.reset_at) > now)]
            exhausted = state != "UNKNOWN" and any(p.usage_state == "EXHAUSTED" and reliable_now(p.evidence) for p in pools)
            if exhausted:
                state = "EXHAUSTED"
            elif known and (state == "UNKNOWN" or state in {"READY", "CONSERVE", "RESERVE"}):
                remaining = min(known)
                state = "READY" if remaining > 40 else "CONSERVE" if remaining > 20 else "RESERVE"
        raw_models = raw.get("models", runtime.models)
        raw_capabilities = raw.get("capabilities", runtime.capabilities)
        if isinstance(raw_models, (str, bytes)) or not isinstance(raw_models, (list, tuple)):
            raise BootstrapError("provider metadata models must be a list")
        if isinstance(raw_capabilities, (str, bytes)) or not isinstance(raw_capabilities, (list, tuple)):
            raise BootstrapError("provider metadata capabilities must be a list")
        return ProviderHealth(provider=runtime.provider, state=state, account=raw.get("account"), installed=raw.get("installed"), version=raw.get("version"), authenticated=raw.get("authenticated"), available=raw.get("available"), models=tuple(raw_models), quota_pools=tuple(pools), reset_at=raw.get("reset_at"), last_checked=raw.get("last_checked"), error=raw.get("error"), capabilities=tuple(raw_capabilities), evidence=top_evidence)
    runtimes = tuple(replace(runtime, health=health_for(runtime)) for runtime in runtimes)
    available = any(runtime.status == "AVAILABLE" for runtime in runtimes)
    if workspace.status in {"NOT_CONFIGURED", "MISSING", "INVALID"}:
        overall_status = "SETUP_REQUIRED"
    elif available:
        overall_status = "READY"
    elif probe:
        overall_status = "RUNTIME_REQUIRED"
    else:
        overall_status = "CHECK_REQUIRED"
    next_steps: list[str] = []
    if workspace.status != "READY":
        next_steps.append("먼저 검토된 ZIP을 stage할 workspace 폴더를 지정하세요.")
    if not probe:
        next_steps.append("연결 점검을 눌러 LM Studio/Ollama의 로컬 응답을 확인하세요.")
    elif not available:
        next_steps.append("LM Studio 또는 Ollama 앱을 실행한 뒤 다시 점검하세요.")
    else:
        next_steps.append("runtime이 확인되었습니다. 다음 단계는 명시적 승인 후 Candidate 실행입니다.")
    return ConnectionReport(
        workspace=workspace,
        runtimes=runtimes,
        overall_status=overall_status,
        next_steps=tuple(next_steps),
        network_probed=probe,
        executors=_build_executor_descriptors(runtimes),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Synapse workspace and local runtime connections")
    parser.add_argument("--workspace", default="", help="staged workspace directory")
    parser.add_argument("--no-network", action="store_true", help="skip local runtime HTTP probes")
    parser.add_argument("--timeout", type=float, default=0.8, help="probe timeout in seconds")
    parser.add_argument("--provider-metadata", default="", help="explicit provider health JSON file")
    args = parser.parse_args()
    metadata = {}
    if args.provider_metadata:
        try:
            metadata = json.loads(Path(args.provider_metadata).read_text(encoding="utf-8"))
            if not isinstance(metadata, Mapping):
                raise ValueError("provider metadata must be a JSON object")  # noqa: TRY004 - CLI validation is translated to argparse's input error
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            parser.error(f"invalid provider metadata: {exc}")
    try:
        report = build_connection_report(args.workspace, probe=not args.no_network, timeout_seconds=args.timeout, provider_metadata=metadata)
    except (BootstrapError, ValueError) as exc:
        parser.error(f"invalid provider metadata: {exc}")
    print(json.dumps(report.to_record(), ensure_ascii=False, indent=2))
    return 0 if report.overall_status in {"READY", "CHECK_REQUIRED"} else 1


__all__ = [
    "BootstrapError",
    "ConnectionReport",
    "ExecutorDescriptor",
    "RuntimeProbe",
    "WorkspaceHandshake",
    "build_connection_report",
    "handshake_workspace",
    "is_text_generation_model",
]


if __name__ == "__main__":
    raise SystemExit(main())
