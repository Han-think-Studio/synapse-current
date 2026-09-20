"""Provider-neutral runtime contracts and local provider adapters."""

from synapse.runtime.antigravity import AntigravityAdapter, AntigravityBrokerTransport
from synapse.runtime.antigravity_broker import (
    ALLOWED_OPERATIONS,
    AntigravityBroker,
    BrokerProtocolError,
    BrokerRequest,
    BrokerResult,
    invoke_named_pipe,
    provider_health,
    serve_named_pipe,
)
from synapse.runtime.cli import (
    ANTIGRAVITY_AUTH_CONTEXT_STATES,
    ClaudeCodeAdapter,
    build_cli_capability_receipt,
    classify_antigravity_auth_context,
    cli_capability_inventory,
)
from synapse.runtime.contracts import (
    DATA_EGRESS_POLICIES,
    EXECUTION_SCOPES,
    NETWORK_SCOPES,
    PreparedRequest,
    RuntimeAdapter,
    RuntimeAdapterError,
    RuntimeErrorKind,
    RuntimeObservation,
    RuntimeRequest,
    RuntimeResponse,
    is_runtime_adapter,
)
from synapse.runtime.http_transport import JsonTransport, UrllibJsonTransport
from synapse.runtime.lmstudio import LMStudioAdapter
from synapse.runtime.ollama import OllamaAdapter
from synapse.runtime.operation import (
    MAX_RUNTIME_ERROR_DETAIL,
    RUNTIME_OPERATION_SCHEMA,
    RUNTIME_OPERATION_STAGES,
    RUNTIME_OPERATION_STATUSES,
    RuntimeCompleter,
    RuntimeOperationError,
    RuntimeOperationResult,
    run_explicit_runtime_operation,
)
from synapse.runtime.policy import (
    RuntimePolicyError,
    network_scope_for_url,
    validate_endpoint_policy,
)
from synapse.runtime.registry import BUILTIN_ADAPTERS, get_builtin_adapter

__all__ = [
    "ALLOWED_OPERATIONS",
    "ANTIGRAVITY_AUTH_CONTEXT_STATES",
    "BUILTIN_ADAPTERS",
    "DATA_EGRESS_POLICIES",
    "EXECUTION_SCOPES",
    "MAX_RUNTIME_ERROR_DETAIL",
    "NETWORK_SCOPES",
    "RUNTIME_OPERATION_SCHEMA",
    "RUNTIME_OPERATION_STAGES",
    "RUNTIME_OPERATION_STATUSES",
    "AntigravityAdapter",
    "AntigravityBroker",
    "AntigravityBrokerTransport",
    "BrokerProtocolError",
    "BrokerRequest",
    "BrokerResult",
    "ClaudeCodeAdapter",
    "JsonTransport",
    "LMStudioAdapter",
    "OllamaAdapter",
    "PreparedRequest",
    "RuntimeAdapter",
    "RuntimeAdapterError",
    "RuntimeCompleter",
    "RuntimeErrorKind",
    "RuntimeObservation",
    "RuntimeOperationError",
    "RuntimeOperationResult",
    "RuntimePolicyError",
    "RuntimeRequest",
    "RuntimeResponse",
    "UrllibJsonTransport",
    "build_cli_capability_receipt",
    "classify_antigravity_auth_context",
    "cli_capability_inventory",
    "get_builtin_adapter",
    "invoke_named_pipe",
    "is_runtime_adapter",
    "network_scope_for_url",
    "provider_health",
    "run_explicit_runtime_operation",
    "serve_named_pipe",
    "validate_endpoint_policy",
]
