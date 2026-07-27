"""Canonical immutable registrations for trusted built-in tools."""

from app.services.harness.protocol import IdempotencyClass
from app.services.harness.tools.contracts import (
    StrictToolArguments,
    ToolOutputContract,
    ToolOutputOverflow,
    build_tool_descriptor,
)
from app.services.harness.tools.file_contracts import (
    PatchFileArguments,
    ReadFileArguments,
    SearchFilesArguments,
)
from app.services.harness.tools.process_contracts import RunProcessArguments
from app.services.harness.tools.process_service import (
    PROCESS_TOOL_CAPABILITY,
    PROCESS_TOOL_NAME,
    PROCESS_TOOL_VERSION,
)
from app.services.harness.tools.registry import ToolRegistration, ToolRegistry

READ_FILE_TOOL_NAME = "workspace.read_file"
SEARCH_FILES_TOOL_NAME = "workspace.search_files"
PATCH_FILE_TOOL_NAME = "workspace.patch_file"
BUILTIN_TOOL_VERSION = "1.0.0"


def builtin_tool_registry() -> ToolRegistry:
    return ToolRegistry(builtin_tool_registrations())


def builtin_tool_registrations() -> tuple[ToolRegistration, ...]:
    registrations = (
        _registration(
            name=PATCH_FILE_TOOL_NAME,
            aliases=("apply_patch",),
            capability="filesystem.write",
            idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
            argument_model=PatchFileArguments,
            maximum_bytes=64 * 1024,
            maximum_inline_bytes=64 * 1024,
        ),
        _registration(
            name=PROCESS_TOOL_NAME,
            aliases=("run_process",),
            capability=PROCESS_TOOL_CAPABILITY,
            idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
            argument_model=RunProcessArguments,
            maximum_bytes=1024 * 1024,
            maximum_inline_bytes=1024 * 1024,
        ),
        _registration(
            name=READ_FILE_TOOL_NAME,
            aliases=("read_file",),
            capability="filesystem.read",
            idempotency_class=IdempotencyClass.READ_ONLY,
            argument_model=ReadFileArguments,
            maximum_bytes=1024 * 1024,
            maximum_inline_bytes=1024 * 1024,
        ),
        _registration(
            name=SEARCH_FILES_TOOL_NAME,
            aliases=("search_files",),
            capability="filesystem.read",
            idempotency_class=IdempotencyClass.READ_ONLY,
            argument_model=SearchFilesArguments,
            maximum_bytes=16 * 1024 * 1024,
            maximum_inline_bytes=1024 * 1024,
        ),
    )
    return tuple(
        sorted(
            registrations,
            key=lambda registration: (
                registration.descriptor.name,
                registration.descriptor.version,
            ),
        )
    )


def _registration(
    *,
    name: str,
    aliases: tuple[str, ...],
    capability: str,
    idempotency_class: IdempotencyClass,
    argument_model: type[StrictToolArguments],
    maximum_bytes: int,
    maximum_inline_bytes: int,
) -> ToolRegistration:
    descriptor = build_tool_descriptor(
        name=name,
        version=(
            PROCESS_TOOL_VERSION
            if name == PROCESS_TOOL_NAME
            else BUILTIN_TOOL_VERSION
        ),
        aliases=aliases,
        default_version=True,
        capability=capability,
        idempotency_class=idempotency_class,
        argument_model=argument_model,
        output=ToolOutputContract(
            maximum_bytes=maximum_bytes,
            maximum_inline_bytes=maximum_inline_bytes,
            overflow=ToolOutputOverflow.REJECT,
        ),
    )
    return ToolRegistration(
        descriptor=descriptor,
        argument_model=argument_model,
    )
