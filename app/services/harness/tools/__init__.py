"""Typed and bounded Atlas Harness tools."""

from app.services.harness.tools.builtin_executor import ProcessDispatchContext
from app.services.harness.tools.builtins import (
    BUILTIN_TOOL_VERSION,
    PATCH_FILE_TOOL_NAME,
    READ_FILE_TOOL_NAME,
    SEARCH_FILES_TOOL_NAME,
    builtin_tool_registrations,
    builtin_tool_registry,
)
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
from app.services.harness.tools.dispatch_admission import (
    ToolDispatchAdmissionError,
    ToolDispatchAdmissionErrorCode,
)
from app.services.harness.tools.dispatch_contracts import (
    ToolDispatchOutcome,
    ToolDispatchStatus,
)
from app.services.harness.tools.dispatch_service import (
    BuiltinToolDispatcher,
    ToolDispatchDurabilityError,
)
from app.services.harness.tools.file_contracts import (
    PatchFileArguments,
    PatchFileEdit,
    PatchFileResult,
    ReadFileArguments,
    ReadFileResult,
    SearchFilesArguments,
    SearchFilesResult,
    SearchMatch,
)
from app.services.harness.tools.file_errors import (
    WorkspaceFileError,
    WorkspaceFileErrorCode,
)
from app.services.harness.tools.file_service import WorkspaceFileService
from app.services.harness.tools.process_contracts import (
    RunProcessArguments,
    RunProcessResult,
)
from app.services.harness.tools.process_service import (
    PROCESS_TOOL_CAPABILITY,
    PROCESS_TOOL_NAME,
    PROCESS_TOOL_VERSION,
    ProcessToolError,
    ProcessToolErrorCode,
    SandboxedProcessService,
)
from app.services.harness.tools.registry import (
    MAXIMUM_REGISTERED_TOOLS,
    ToolRegistration,
    ToolRegistrationError,
    ToolRegistry,
    ToolRegistryError,
    ToolRegistryErrorCode,
)

__all__ = (
    "BUILTIN_TOOL_VERSION",
    "BuiltinToolDispatcher",
    "DEFAULT_TOOL_OUTPUT_BYTES",
    "MAXIMUM_REGISTERED_TOOLS",
    "MAXIMUM_TOOL_OUTPUT_BYTES",
    "PatchFileArguments",
    "PatchFileEdit",
    "PatchFileResult",
    "PATCH_FILE_TOOL_NAME",
    "PROCESS_TOOL_CAPABILITY",
    "PROCESS_TOOL_NAME",
    "PROCESS_TOOL_VERSION",
    "ProcessToolError",
    "ProcessToolErrorCode",
    "ProcessDispatchContext",
    "ReadFileArguments",
    "ReadFileResult",
    "READ_FILE_TOOL_NAME",
    "SearchFilesArguments",
    "SearchFilesResult",
    "SearchMatch",
    "SEARCH_FILES_TOOL_NAME",
    "RunProcessArguments",
    "RunProcessResult",
    "SandboxedProcessService",
    "StrictToolArguments",
    "ToolDescriptor",
    "ToolDispatchAdmissionError",
    "ToolDispatchAdmissionErrorCode",
    "ToolDispatchDurabilityError",
    "ToolDispatchOutcome",
    "ToolDispatchStatus",
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
    "builtin_tool_registrations",
    "builtin_tool_registry",
    "canonical_tool_schema",
    "tool_descriptor_sha256",
)
