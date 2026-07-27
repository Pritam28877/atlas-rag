"""Canonical permit, profile, and admission evidence for process tools."""

import json
from datetime import timedelta
from pathlib import Path

from app.services.harness.policy import (
    ExecutableTarget,
    FilesystemOperation,
    FilesystemTarget,
)
from app.services.harness.protocol import (
    IdempotencyClass,
    OperationLimits,
)
from app.services.harness.protocol.operation_admission import (
    CanonicalOperationRequest,
    OperationFence,
    canonical_operation_args_sha256,
    stable_operation_id,
)
from app.services.harness.protocol.sandbox import (
    PlatformIsolationCapabilities,
    SandboxIsolationMode,
    SandboxPlatform,
    platform_capabilities_sha256,
)
from app.services.harness.sandbox import (
    SandboxChildEnvironment,
    SandboxProcessResult,
    SandboxResourceLimits,
    compile_sandbox_admission,
    compile_sandbox_profile,
)
from app.services.harness.tools import (
    PROCESS_TOOL_CAPABILITY,
    PROCESS_TOOL_NAME,
    PROCESS_TOOL_VERSION,
    RunProcessArguments,
    ToolOutputContract,
    ToolOutputOverflow,
    ValidatedToolCall,
    build_tool_descriptor,
)
from app.services.harness.tools.operation_lifecycle import (
    DurableOperationLifecycle,
)
from tests.harness.operations.fixtures import NOW, RecordingStore
from tests.harness.sandbox.fixtures import grant

WORKSPACE_ID = "wsp_" + "a" * 32
TURN_ID = "trn_" + "b" * 32


class RecordingSupervisor:
    def __init__(
        self,
        outcome: SandboxProcessResult | BaseException,
    ) -> None:
        self.outcome = outcome
        self.calls = 0

    async def run(
        self,
        profile,
        environment,
        *,
        cancellation,
    ) -> SandboxProcessResult:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def process_arguments() -> RunProcessArguments:
    return RunProcessArguments(
        executable="/usr/bin/python3",
        arguments=("-c", "print('safe')"),
        working_directory="work",
    )


def process_call(
    *,
    arguments: RunProcessArguments | None = None,
    maximum_bytes: int = 256,
    maximum_inline_bytes: int = 64,
) -> ValidatedToolCall:
    values = arguments or process_arguments()
    descriptor = build_tool_descriptor(
        name=PROCESS_TOOL_NAME,
        version=PROCESS_TOOL_VERSION,
        aliases=("run_process",),
        default_version=True,
        capability=PROCESS_TOOL_CAPABILITY,
        idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
        argument_model=RunProcessArguments,
        output=ToolOutputContract(
            maximum_bytes=maximum_bytes,
            maximum_inline_bytes=maximum_inline_bytes,
            overflow=ToolOutputOverflow.REJECT,
        ),
    )
    arguments_value = values.model_dump(mode="json")
    arguments_json = json.dumps(
        arguments_value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ValidatedToolCall(
        call_id="call-process-1",
        requested_name=PROCESS_TOOL_NAME,
        tool_name=PROCESS_TOOL_NAME,
        tool_version=PROCESS_TOOL_VERSION,
        descriptor_sha256=descriptor.descriptor_sha256,
        arguments_json=arguments_json,
        args_sha256=canonical_operation_args_sha256(arguments_value),
        capability=PROCESS_TOOL_CAPABILITY,
        idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
        output=descriptor.output,
    )


async def process_evidence(
    workspace: Path,
    *,
    call: ValidatedToolCall | None = None,
    profile_output_bytes: int = 256,
):
    verified_call = call or process_call()
    operation_id = stable_operation_id(
        workspace_id=WORKSPACE_ID,
        turn_id=TURN_ID,
        idempotency_key="process-command-0001",
        tool_name=verified_call.tool_name,
        tool_version=verified_call.tool_version,
        args_sha256=verified_call.args_sha256,
        attempt=1,
    )
    request = CanonicalOperationRequest(
        workspace_id=WORKSPACE_ID,
        turn_id=TURN_ID,
        idempotency_key="process-command-0001",
        idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
        attempt=1,
        tool_name=verified_call.tool_name,
        tool_version=verified_call.tool_version,
        args_sha256=verified_call.args_sha256,
        capability=verified_call.capability,
        policy_decision_id="dcs_" + "c" * 32,
        approval_id="apr_" + "d" * 32,
        limits=OperationLimits(
            max_duration_ms=1000,
            max_cpu_ms=1000,
            max_memory_bytes=64 * 1024 * 1024,
            max_output_bytes=256,
            max_processes=4,
        ),
        operation_id=operation_id,
    )
    fence = OperationFence(
        workspace_id=WORKSPACE_ID,
        operation_id=operation_id,
        lease_sha256="e" * 64,
        fencing_token=3,
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=1),
    )
    lifecycle = DurableOperationLifecycle(RecordingStore())
    prepared = await lifecycle.prepare(request, fence, prepared_at=NOW)
    permit = await lifecycle.dispatch(
        prepared,
        fence,
        dispatched_at=NOW + timedelta(seconds=1),
    )

    (workspace / "work").mkdir()
    arguments = RunProcessArguments.model_validate_json(
        verified_call.arguments_json
    )
    executable = ExecutableTarget(
        executable=arguments.executable,
        arguments=arguments.arguments,
        working_directory=arguments.working_directory,
    )
    profile = compile_sandbox_profile(
        operation_id=operation_id,
        workspace=workspace,
        grants=(
            grant(executable, PROCESS_TOOL_CAPABILITY),
            grant(
                FilesystemTarget(
                    root_uri="file:///workspace",
                    relative_path="work",
                    operation=FilesystemOperation.READ,
                ),
                "filesystem.read",
            ),
        ),
        default_limits=SandboxResourceLimits(
            wall_time_ms=1000,
            cpu_time_ms=1000,
            memory_bytes=64 * 1024 * 1024,
            output_bytes=profile_output_bytes,
            processes=4,
        ),
    )
    capabilities = production_capabilities()
    admission = compile_sandbox_admission(
        capabilities,
        profile,
        side_effecting=True,
    )
    environment = SandboxChildEnvironment(
        operation_id=operation_id,
        destination_sha256=profile.destination_sha256,
        parent_environment={},
        secret_environment={},
        receipt=None,
    )
    return verified_call, permit, profile, admission, environment


def production_capabilities() -> PlatformIsolationCapabilities:
    values = {
        "platform": SandboxPlatform.LINUX,
        "mode": SandboxIsolationMode.PRODUCTION,
        "native_backend": "test-production",
        "containment_available": True,
        "filesystem_isolation": True,
        "process_tree_isolation": True,
        "network_isolation": True,
        "syscall_filtering": True,
        "resource_limits_enforced": True,
        "cgroup_v2": True,
        "cgroup_delegated": True,
        "proxy_egress": False,
        "side_effects_supported": True,
        "reasons": (),
        "detected_at": NOW,
    }
    return PlatformIsolationCapabilities(
        **values,
        capabilities_sha256=platform_capabilities_sha256(**values),
    )
