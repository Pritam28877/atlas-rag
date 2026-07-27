"""Permit-bound process execution through the sandbox supervisor only."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.services.harness.policy import ExecutableTarget
from app.services.harness.protocol import IdempotencyClass
from app.services.harness.protocol.operation_admission import (
    OperationDispatchPermit,
)
from app.services.harness.protocol.sandbox import (
    SandboxAdmissionDecision,
    SandboxAdmissionOutcome,
    SandboxIsolationMode,
)
from app.services.harness.sandbox import (
    SandboxChildEnvironment,
    SandboxOutputLimitExceeded,
    SandboxProcessCancelled,
    SandboxProcessResult,
    SandboxProcessTimedOut,
    SandboxProfile,
    SandboxSupervisorError,
    sandbox_destination_sha256,
)
from app.services.harness.tools.contracts import ValidatedToolCall
from app.services.harness.tools.process_contracts import (
    RunProcessArguments,
    RunProcessResult,
)
from app.services.harness.tools.process_output import normalize_process_result

PROCESS_TOOL_NAME = "process.run"
PROCESS_TOOL_VERSION = "1.0.0"
PROCESS_TOOL_CAPABILITY = "executable.run"


class ProcessToolErrorCode(StrEnum):
    CANCELLED = "cancelled"
    EXECUTION = "execution"
    OUTPUT_LIMIT = "output_limit"
    PERMIT = "permit"
    TIMEOUT = "timeout"


class ProcessToolError(RuntimeError):
    def __init__(self, code: ProcessToolErrorCode) -> None:
        super().__init__("process tool execution failed")
        self.code = code


class ProcessSandboxSupervisor(Protocol):
    async def run(
        self,
        profile: SandboxProfile,
        environment: SandboxChildEnvironment,
        *,
        cancellation: asyncio.Event,
    ) -> SandboxProcessResult: ...


class SandboxedProcessService:
    def __init__(self, supervisor: ProcessSandboxSupervisor) -> None:
        self._supervisor = supervisor

    async def run(
        self,
        call: ValidatedToolCall,
        permit: OperationDispatchPermit,
        profile: SandboxProfile,
        admission: SandboxAdmissionDecision,
        environment: SandboxChildEnvironment,
        *,
        cancellation: asyncio.Event,
    ) -> RunProcessResult:
        (
            verified_call,
            verified_permit,
            verified_profile,
        ) = _validate_evidence(call, permit, profile, admission)
        _validate_environment(environment, verified_profile)
        try:
            result = await self._supervisor.run(
                verified_profile,
                environment,
                cancellation=cancellation,
            )
        except asyncio.CancelledError:
            raise ProcessToolError(ProcessToolErrorCode.CANCELLED) from None
        except SandboxProcessCancelled:
            raise ProcessToolError(ProcessToolErrorCode.CANCELLED) from None
        except SandboxProcessTimedOut:
            raise ProcessToolError(ProcessToolErrorCode.TIMEOUT) from None
        except SandboxOutputLimitExceeded:
            raise ProcessToolError(ProcessToolErrorCode.OUTPUT_LIMIT) from None
        except SandboxSupervisorError:
            raise ProcessToolError(ProcessToolErrorCode.EXECUTION) from None
        except Exception:
            raise ProcessToolError(ProcessToolErrorCode.EXECUTION) from None
        if (
            not isinstance(result.return_code, int)
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or len(result.stdout) + len(result.stderr)
            > verified_profile.limits.output_bytes
        ):
            raise ProcessToolError(ProcessToolErrorCode.EXECUTION)
        return normalize_process_result(
            result,
            maximum_inline_bytes=verified_call.output.maximum_inline_bytes,
        )


def _validate_evidence(
    call: ValidatedToolCall,
    permit: OperationDispatchPermit,
    profile: SandboxProfile,
    admission: SandboxAdmissionDecision,
) -> tuple[
    ValidatedToolCall,
    OperationDispatchPermit,
    SandboxProfile,
]:
    try:
        verified_call = ValidatedToolCall.model_validate(call.model_dump())
        verified_permit = OperationDispatchPermit.model_validate(
            permit.model_dump()
        )
        verified_profile = SandboxProfile.model_validate(profile.model_dump())
        verified_admission = SandboxAdmissionDecision.model_validate(
            admission.model_dump()
        )
        arguments = RunProcessArguments.model_validate_json(
            verified_call.arguments_json
        )
        target = ExecutableTarget(
            executable=arguments.executable,
            arguments=arguments.arguments,
            working_directory=arguments.working_directory,
        )
    except Exception:
        raise ProcessToolError(ProcessToolErrorCode.PERMIT) from None
    operation = verified_permit.operation
    prepared = verified_permit.prepared_receipt
    dispatched = verified_permit.dispatched_receipt
    if (
        verified_call.tool_name != PROCESS_TOOL_NAME
        or verified_call.tool_version != PROCESS_TOOL_VERSION
        or verified_call.capability != PROCESS_TOOL_CAPABILITY
        or verified_call.idempotency_class is not IdempotencyClass.NON_IDEMPOTENT
        or operation.tool_name != verified_call.tool_name
        or operation.tool_version != verified_call.tool_version
        or operation.args_sha256 != verified_call.args_sha256
        or operation.capability != verified_call.capability
        or operation.idempotency_class is not verified_call.idempotency_class
        or prepared.operation_id != operation.operation_id
        or dispatched.operation_id != operation.operation_id
        or prepared.workspace_id != dispatched.workspace_id
        or prepared.fencing_token != operation.lease_fencing_token
        or dispatched.fencing_token != operation.lease_fencing_token
        or verified_profile.operation_id != operation.operation_id
        or verified_profile.executable != arguments.executable
        or not _executable_source_matches(
            verified_profile,
            arguments.executable,
        )
        or verified_profile.arguments != arguments.arguments
        or verified_profile.working_directory
        != _sandbox_working_directory(arguments.working_directory)
        or verified_profile.destination_sha256
        != sandbox_destination_sha256(
            operation_id=operation.operation_id,
            executable=target,
        )
        or verified_admission.outcome is not SandboxAdmissionOutcome.ADMITTED
        or verified_admission.mode is not SandboxIsolationMode.PRODUCTION
        or not verified_admission.side_effecting
        or verified_admission.profile_sha256 != verified_profile.profile_sha256
        or not _limits_are_narrowed(
            verified_call,
            verified_permit,
            verified_profile,
        )
    ):
        raise ProcessToolError(ProcessToolErrorCode.PERMIT)
    return (
        verified_call,
        verified_permit,
        verified_profile,
    )


def _limits_are_narrowed(
    call: ValidatedToolCall,
    permit: OperationDispatchPermit,
    profile: SandboxProfile,
) -> bool:
    profile_limits = profile.limits
    operation_limits = permit.operation.limits
    return (
        profile_limits.wall_time_ms <= operation_limits.max_duration_ms
        and profile_limits.cpu_time_ms <= operation_limits.max_cpu_ms
        and profile_limits.memory_bytes <= operation_limits.max_memory_bytes
        and profile_limits.output_bytes <= operation_limits.max_output_bytes
        and profile_limits.output_bytes <= call.output.maximum_bytes
        and profile_limits.processes <= operation_limits.max_processes
    )


def _validate_environment(
    environment: SandboxChildEnvironment,
    profile: SandboxProfile,
) -> None:
    if (
        environment.operation_id != profile.operation_id
        or environment.destination_sha256 != profile.destination_sha256
    ):
        raise ProcessToolError(ProcessToolErrorCode.PERMIT)


def _sandbox_working_directory(value: str) -> str:
    return "/workspace" if value in {"", "."} else f"/workspace/{value}"


def _executable_source_matches(
    profile: SandboxProfile,
    executable: str,
) -> bool:
    try:
        return Path(executable).resolve(strict=True) == profile.executable_source
    except OSError:
        return False
