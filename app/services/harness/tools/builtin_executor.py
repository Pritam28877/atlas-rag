"""Typed routing from a validated built-in call to bounded primitives."""

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel

from app.services.harness.protocol.operation_admission import (
    OperationDispatchPermit,
)
from app.services.harness.protocol.sandbox import SandboxAdmissionDecision
from app.services.harness.sandbox import SandboxChildEnvironment, SandboxProfile
from app.services.harness.tools.builtins import (
    PATCH_FILE_TOOL_NAME,
    READ_FILE_TOOL_NAME,
    SEARCH_FILES_TOOL_NAME,
)
from app.services.harness.tools.contracts import ValidatedToolCall
from app.services.harness.tools.file_contracts import (
    PatchFileArguments,
    ReadFileArguments,
    SearchFilesArguments,
)
from app.services.harness.tools.file_service import WorkspaceFileService
from app.services.harness.tools.process_service import (
    PROCESS_TOOL_NAME,
    ProcessToolError,
    ProcessToolErrorCode,
    SandboxedProcessService,
)


@dataclass(frozen=True, slots=True)
class ProcessDispatchContext:
    profile: SandboxProfile
    admission: SandboxAdmissionDecision
    environment: SandboxChildEnvironment


class BuiltinToolExecutor:
    def __init__(
        self,
        files: WorkspaceFileService,
        processes: SandboxedProcessService,
    ) -> None:
        self._files = files
        self._processes = processes

    async def execute(
        self,
        call: ValidatedToolCall,
        permit: OperationDispatchPermit,
        *,
        cancellation: asyncio.Event,
        process: ProcessDispatchContext | None,
    ) -> BaseModel:
        timeout_ms = permit.operation.limits.max_duration_ms
        if call.tool_name == READ_FILE_TOOL_NAME:
            read_arguments = ReadFileArguments.model_validate_json(
                call.arguments_json
            )
            return await self._files.read(
                read_arguments,
                cancellation=cancellation,
                timeout_ms=timeout_ms,
            )
        if call.tool_name == SEARCH_FILES_TOOL_NAME:
            search_arguments = SearchFilesArguments.model_validate_json(
                call.arguments_json
            )
            return await self._files.search(
                search_arguments,
                cancellation=cancellation,
                timeout_ms=timeout_ms,
            )
        if call.tool_name == PATCH_FILE_TOOL_NAME:
            patch_arguments = PatchFileArguments.model_validate_json(
                call.arguments_json
            )
            return await self._files.patch(
                patch_arguments,
                cancellation=cancellation,
                timeout_ms=timeout_ms,
            )
        if call.tool_name == PROCESS_TOOL_NAME and process is not None:
            return await self._processes.run(
                call,
                permit,
                process.profile,
                process.admission,
                process.environment,
                cancellation=cancellation,
            )
        raise ProcessToolError(ProcessToolErrorCode.PERMIT)
