"""Typed and bounded Atlas Harness tools."""

from app.services.harness.tools.contracts import (
    DEFAULT_TOOL_OUTPUT_BYTES,
    MAXIMUM_TOOL_OUTPUT_BYTES,
    StrictToolArguments,
    ToolDescriptor,
    ToolOutputContract,
    ToolOutputOverflow,
    ValidatedToolCall,
    build_tool_descriptor,
    canonical_tool_schema,
    tool_descriptor_sha256,
)
from app.services.harness.tools.file_contracts import (
    ReadFileArguments,
    ReadFileResult,
    SearchFilesArguments,
    SearchFilesResult,
    SearchMatch,
)
from app.services.harness.tools.file_service import WorkspaceFileService
from app.services.harness.tools.registry import (
    MAXIMUM_REGISTERED_TOOLS,
    ToolRegistration,
    ToolRegistrationError,
    ToolRegistry,
    ToolRegistryError,
    ToolRegistryErrorCode,
)
from app.services.harness.tools.workspace_descriptor import (
    WorkspaceFileError,
    WorkspaceFileErrorCode,
)

__all__ = (
    "DEFAULT_TOOL_OUTPUT_BYTES",
    "MAXIMUM_REGISTERED_TOOLS",
    "MAXIMUM_TOOL_OUTPUT_BYTES",
    "ReadFileArguments",
    "ReadFileResult",
    "SearchFilesArguments",
    "SearchFilesResult",
    "SearchMatch",
    "StrictToolArguments",
    "ToolDescriptor",
    "ToolOutputContract",
    "ToolOutputOverflow",
    "ToolRegistration",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolRegistryErrorCode",
    "ValidatedToolCall",
    "WorkspaceFileError",
    "WorkspaceFileErrorCode",
    "WorkspaceFileService",
    "build_tool_descriptor",
    "canonical_tool_schema",
    "tool_descriptor_sha256",
)
