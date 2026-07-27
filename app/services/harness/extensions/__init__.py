"""Out-of-process Atlas Harness extensions and hooks."""

from app.services.harness.extensions.hook_contracts import (
    HookDescriptor,
    HookDispatchResult,
    HookEvent,
    HookFailurePolicy,
    HookLifecycleEvent,
    HookMode,
    HookOutcome,
    HookResponse,
)
from app.services.harness.extensions.hook_dispatcher import (
    HookDispatcher,
    HookDispatchError,
    HookDispatchErrorCode,
    HookRegistration,
    HookTransport,
)
from app.services.harness.extensions.mcp_contracts import (
    MCP_DEFAULT_PENDING_CALLS,
    MCP_HARD_PENDING_CALLS,
    MCP_PROTOCOL_VERSION,
    McpCallRequest,
    McpCallResult,
    McpEgressPolicy,
    McpInitializeRequest,
    McpNegotiatedSession,
    McpResponseEnvelope,
    McpServerDescriptor,
    McpTransportKind,
)
from app.services.harness.extensions.mcp_host import (
    McpHost,
    McpHostError,
    McpHostErrorCode,
    McpTransport,
)
from app.services.harness.extensions.mcp_transports import (
    McpHttpChannel,
    McpHttpTransport,
    McpStdioChannel,
    McpStdioTransport,
)
from app.services.harness.extensions.plugin_contracts import (
    PluginCall,
    PluginDescriptor,
    PluginResult,
)
from app.services.harness.extensions.plugin_host import (
    PluginHost,
    PluginHostError,
    PluginHostErrorCode,
    PluginTransport,
)

__all__ = (
    "MCP_DEFAULT_PENDING_CALLS",
    "MCP_HARD_PENDING_CALLS",
    "MCP_PROTOCOL_VERSION",
    "McpCallRequest",
    "McpCallResult",
    "McpEgressPolicy",
    "McpHttpChannel",
    "McpHttpTransport",
    "McpHost",
    "McpHostError",
    "McpHostErrorCode",
    "McpInitializeRequest",
    "McpNegotiatedSession",
    "McpResponseEnvelope",
    "McpServerDescriptor",
    "McpStdioChannel",
    "McpStdioTransport",
    "McpTransport",
    "McpTransportKind",
    "HookDescriptor",
    "HookDispatchError",
    "HookDispatchErrorCode",
    "HookDispatchResult",
    "HookDispatcher",
    "HookEvent",
    "HookFailurePolicy",
    "HookLifecycleEvent",
    "HookMode",
    "HookOutcome",
    "HookRegistration",
    "HookResponse",
    "HookTransport",
    "PluginCall",
    "PluginDescriptor",
    "PluginHost",
    "PluginHostError",
    "PluginHostErrorCode",
    "PluginResult",
    "PluginTransport",
)
