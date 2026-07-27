"""Fail-closed process evidence and limit tests."""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.protocol.sandbox import (
    SandboxAdmissionOutcome,
    SandboxIsolationMode,
    sandbox_admission_sha256,
)
from app.services.harness.sandbox import (
    SandboxChildEnvironment,
    SandboxProcessResult,
)
from app.services.harness.tools import (
    ProcessToolError,
    ProcessToolErrorCode,
    RunProcessArguments,
    SandboxedProcessService,
)
from tests.harness.tools.process_fixtures import (
    RecordingSupervisor,
    process_call,
    process_evidence,
)


def test_denied_admission_never_reaches_supervisor(tmp_path: Path) -> None:
    async def scenario() -> None:
        call, permit, profile, admission, environment = (
            await process_evidence(tmp_path.resolve())
        )
        values = {
            **admission.model_dump(exclude={"decision_sha256"}),
            "outcome": SandboxAdmissionOutcome.DENIED,
            "reason": "Denied for test.",
        }
        denied = admission.model_validate(
            {
                **values,
                "decision_sha256": sandbox_admission_sha256(**values),
            }
        )
        supervisor = RecordingSupervisor(
            SandboxProcessResult(return_code=0, stdout=b"", stderr=b"")
        )
        with pytest.raises(ProcessToolError) as failure:
            await SandboxedProcessService(supervisor).run(
                call,
                permit,
                profile,
                denied,
                environment,
                cancellation=asyncio.Event(),
            )
        assert failure.value.code is ProcessToolErrorCode.PERMIT
        assert supervisor.calls == 0

    asyncio.run(scenario())


def test_profile_output_cannot_exceed_call_or_permit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        call = process_call(maximum_bytes=128)
        evidence = await process_evidence(
            tmp_path.resolve(),
            call=call,
            profile_output_bytes=129,
        )
        supervisor = RecordingSupervisor(
            SandboxProcessResult(return_code=0, stdout=b"", stderr=b"")
        )
        with pytest.raises(ProcessToolError) as failure:
            await SandboxedProcessService(supervisor).run(
                *evidence,
                cancellation=asyncio.Event(),
            )
        assert failure.value.code is ProcessToolErrorCode.PERMIT
        assert supervisor.calls == 0

    asyncio.run(scenario())


def test_degraded_admission_and_wrong_environment_are_denied(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        call, permit, profile, admission, environment = (
            await process_evidence(tmp_path.resolve())
        )
        degraded_values = {
            **admission.model_dump(exclude={"decision_sha256"}),
            "mode": SandboxIsolationMode.READ_ONLY_DEGRADED,
            "side_effecting": False,
            "reason": "Read-only degraded test admission.",
        }
        degraded = admission.model_validate(
            {
                **degraded_values,
                "decision_sha256": sandbox_admission_sha256(
                    **degraded_values
                ),
            }
        )
        wrong_environment = SandboxChildEnvironment(
            operation_id=environment.operation_id,
            destination_sha256="0" * 64,
            parent_environment={},
            secret_environment={},
            receipt=None,
        )
        supervisor = RecordingSupervisor(
            SandboxProcessResult(return_code=0, stdout=b"", stderr=b"")
        )
        for admission_value, environment_value in (
            (degraded, environment),
            (admission, wrong_environment),
        ):
            with pytest.raises(ProcessToolError) as failure:
                await SandboxedProcessService(supervisor).run(
                    call,
                    permit,
                    profile,
                    admission_value,
                    environment_value,
                    cancellation=asyncio.Event(),
                )
            assert failure.value.code is ProcessToolErrorCode.PERMIT
        assert supervisor.calls == 0

    asyncio.run(scenario())


def test_supervisor_cannot_return_more_than_profile_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        evidence = await process_evidence(tmp_path.resolve())
        supervisor = RecordingSupervisor(
            SandboxProcessResult(
                return_code=0,
                stdout=b"x" * 257,
                stderr=b"",
            )
        )
        with pytest.raises(ProcessToolError) as failure:
            await SandboxedProcessService(supervisor).run(
                *evidence,
                cancellation=asyncio.Event(),
            )
        assert failure.value.code is ProcessToolErrorCode.EXECUTION

    asyncio.run(scenario())


def test_valid_but_different_call_cannot_reuse_permit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _, permit, profile, admission, environment = (
            await process_evidence(tmp_path.resolve())
        )
        different_call = process_call(
            arguments=RunProcessArguments(
                executable="/usr/bin/python3",
                arguments=("-c", "print('different')"),
                working_directory="work",
            )
        )
        supervisor = RecordingSupervisor(
            SandboxProcessResult(return_code=0, stdout=b"", stderr=b"")
        )
        with pytest.raises(ProcessToolError) as failure:
            await SandboxedProcessService(supervisor).run(
                different_call,
                permit,
                profile,
                admission,
                environment,
                cancellation=asyncio.Event(),
            )
        assert failure.value.code is ProcessToolErrorCode.PERMIT
        assert supervisor.calls == 0

    asyncio.run(scenario())


def test_shell_string_cannot_replace_structured_process_arguments() -> None:
    with pytest.raises(ValidationError):
        RunProcessArguments.model_validate(
            {
                "command": "python -c 'print(1)'",
                "working_directory": "work",
            }
        )
