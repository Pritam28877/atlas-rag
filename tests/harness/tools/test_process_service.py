"""Permit-bound sandbox process service behavior."""

import asyncio
import hashlib
from pathlib import Path

import pytest

from app.services.harness.sandbox import (
    SandboxOutputLimitExceeded,
    SandboxProcessCancelled,
    SandboxProcessResult,
    SandboxProcessTimedOut,
    SandboxSupervisorError,
)
from app.services.harness.tools import (
    ProcessToolError,
    ProcessToolErrorCode,
    SandboxedProcessService,
)
from tests.harness.tools.process_fixtures import (
    RecordingSupervisor,
    process_evidence,
)


def test_process_runs_only_after_exact_evidence_and_normalizes_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        evidence = await process_evidence(tmp_path.resolve())
        stdout = b"ok\x1b\n"
        stderr = b"\xfferror"
        supervisor = RecordingSupervisor(
            SandboxProcessResult(
                return_code=7,
                stdout=stdout,
                stderr=stderr,
            )
        )
        result = await SandboxedProcessService(supervisor).run(
            *evidence,
            cancellation=asyncio.Event(),
        )

        assert result.return_code == 7
        assert result.stdout == "ok\ufffd\n"
        assert result.stderr == "\ufffderror"
        assert result.stdout_sha256 == hashlib.sha256(stdout).hexdigest()
        assert result.stderr_sha256 == hashlib.sha256(stderr).hexdigest()
        assert result.encoding_replaced
        assert not result.truncated
        assert supervisor.calls == 1

    asyncio.run(scenario())


def test_process_output_is_bounded_to_inline_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        evidence = await process_evidence(tmp_path.resolve())
        supervisor = RecordingSupervisor(
            SandboxProcessResult(
                return_code=0,
                stdout=b"x" * 60,
                stderr=b"y" * 20,
            )
        )
        result = await SandboxedProcessService(supervisor).run(
            *evidence,
            cancellation=asyncio.Event(),
        )

        assert result.stdout == "x" * 60
        assert result.stderr == "y" * 4
        assert result.output_bytes == 64
        assert result.truncated

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("sandbox_error", "expected_code"),
    (
        (
            SandboxProcessCancelled("sensitive-cancel-detail"),
            ProcessToolErrorCode.CANCELLED,
        ),
        (
            SandboxProcessTimedOut("sensitive-timeout-detail"),
            ProcessToolErrorCode.TIMEOUT,
        ),
        (
            SandboxOutputLimitExceeded("sensitive-limit-detail"),
            ProcessToolErrorCode.OUTPUT_LIMIT,
        ),
        (
            SandboxSupervisorError("sensitive-failure-detail"),
            ProcessToolErrorCode.EXECUTION,
        ),
        (
            RuntimeError("sensitive-unexpected-detail"),
            ProcessToolErrorCode.EXECUTION,
        ),
        (
            asyncio.CancelledError("sensitive-task-cancel-detail"),
            ProcessToolErrorCode.CANCELLED,
        ),
    ),
)
def test_process_maps_sandbox_failures_without_details(
    tmp_path: Path,
    sandbox_error: BaseException,
    expected_code: ProcessToolErrorCode,
) -> None:
    async def scenario() -> None:
        evidence = await process_evidence(tmp_path.resolve())
        with pytest.raises(ProcessToolError) as failure:
            await SandboxedProcessService(
                RecordingSupervisor(sandbox_error)
            ).run(
                *evidence,
                cancellation=asyncio.Event(),
            )
        assert failure.value.code is expected_code
        assert str(sandbox_error) not in str(failure.value)

    asyncio.run(scenario())
