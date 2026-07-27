"""Owned sandbox process lifecycle tests."""

import asyncio
import sys
from pathlib import Path

import pytest

from app.services.harness.policy import FilesystemOperation, FilesystemTarget
from app.services.harness.sandbox import (
    SandboxChildEnvironment,
    SandboxOutputLimitExceeded,
    SandboxProcessCancelled,
    SandboxProcessTimedOut,
    SandboxSupervisor,
    compile_sandbox_profile,
)
from tests.harness.sandbox.fixtures import (
    OPERATION_ID,
    defaults,
    executable_target,
    grant,
)


class FakeCommandBuilder:
    def __init__(self, child_code: str) -> None:
        self.child_code = child_code

    def build(
        self,
        profile,
        *,
        admission_fifo,
        secret_fifo,
        parent_environment,
    ):
        return [
            sys.executable,
            "-c",
            self.child_code,
            str(admission_fifo),
            str(secret_fifo),
        ]


class Lease:
    def __init__(self) -> None:
        self.killed = False
        self.closed = False

    async def kill(self) -> None:
        self.killed = True

    async def close(self) -> None:
        self.closed = True


class Controller:
    def __init__(self, pre_admission_marker: Path | None = None) -> None:
        self.pre_admission_marker = pre_admission_marker
        self.lease = Lease()
        self.process_id: int | None = None
        self.leases: list[Lease] = []
        self.process_ids: list[int] = []

    async def admit(self, process_id, limits):
        self.process_id = process_id
        self.process_ids.append(process_id)
        if self.pre_admission_marker is not None:
            assert not self.pre_admission_marker.exists()
        self.lease = Lease()
        self.leases.append(self.lease)
        return self.lease


def profile(tmp_path: Path, **limit_updates: int):
    work = tmp_path / "work"
    work.mkdir()
    limits = defaults().model_copy(update=limit_updates)
    return compile_sandbox_profile(
        operation_id=OPERATION_ID,
        workspace=tmp_path,
        grants=(
            grant(executable_target(), "executable.run"),
            grant(
                FilesystemTarget(
                    root_uri="file:///workspace",
                    relative_path="work",
                    operation=FilesystemOperation.WRITE,
                ),
                "filesystem.write",
            ),
        ),
        default_limits=limits,
    )


def environment(profile):
    return SandboxChildEnvironment(
        operation_id=profile.operation_id,
        destination_sha256=profile.destination_sha256,
        parent_environment={},
        secret_environment={},
        receipt=None,
    )


def child(code_after_admission: str) -> str:
    return f"""
import sys
with open(sys.argv[1], "rb", buffering=0) as admission:
    assert admission.read(1) == b"1"
with open(sys.argv[2], "rb", buffering=0) as secrets:
    assert secrets.read(2) == b"\\x00\\x00"
{code_after_admission}
"""


def test_resource_admission_precedes_child_and_result_is_bounded(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "started"
    controller = Controller(marker)
    sandbox_profile = profile(tmp_path)
    supervisor = SandboxSupervisor(
        FakeCommandBuilder(
            child(
                f"from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('started'); "
                "print('ok')"
            )
        ),
        controller,
    )

    result = asyncio.run(
        supervisor.run(
            sandbox_profile,
            environment(sandbox_profile),
            cancellation=asyncio.Event(),
        )
    )

    assert result.return_code == 0
    assert result.stdout == b"ok\n"
    assert controller.process_id is not None
    assert controller.lease.closed


def test_output_limit_terminates_and_reaps_process(tmp_path: Path) -> None:
    controller = Controller()
    sandbox_profile = profile(tmp_path, output_bytes=32)
    supervisor = SandboxSupervisor(
        FakeCommandBuilder(
            child(
                "import os; "
                "os.write(1, b'x' * 20); "
                "os.write(2, b'y' * 20)"
            )
        ),
        controller,
    )

    with pytest.raises(SandboxOutputLimitExceeded):
        asyncio.run(
            supervisor.run(
                sandbox_profile,
                environment(sandbox_profile),
                cancellation=asyncio.Event(),
            )
        )

    assert controller.lease.closed


@pytest.mark.parametrize(
    ("cancel", "expected_error"),
    (
        (False, SandboxProcessTimedOut),
        (True, SandboxProcessCancelled),
    ),
)
def test_timeout_and_cancellation_reap_descendants(
    tmp_path: Path,
    cancel: bool,
    expected_error,
) -> None:
    leak_marker = tmp_path / "leaked"
    sandbox_profile = profile(tmp_path, wall_time_ms=200)
    code = child(
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', "
        f"\"import time; from pathlib import Path; time.sleep(0.6); "
        f"Path({str(leak_marker)!r}).write_text('leaked')\"]); "
        "time.sleep(5)"
    )
    controller = Controller()
    cancellation = asyncio.Event()

    async def scenario() -> None:
        if cancel:
            async def cancel_soon() -> None:
                await asyncio.sleep(0.05)
                cancellation.set()

            cancellation_driver = asyncio.create_task(cancel_soon())
        else:
            cancellation_driver = None
        with pytest.raises(expected_error):
            await SandboxSupervisor(
                FakeCommandBuilder(code),
                controller,
            ).run(
                sandbox_profile,
                environment(sandbox_profile),
                cancellation=cancellation,
            )
        if cancellation_driver is not None:
            await cancellation_driver
        await asyncio.sleep(0.8)

    asyncio.run(scenario())
    assert not leak_marker.exists()
    assert controller.lease.closed


def test_repeated_runs_leave_no_process_or_descriptor_growth(
    tmp_path: Path,
) -> None:
    sandbox_profile = profile(tmp_path)
    controller = Controller()
    supervisor = SandboxSupervisor(
        FakeCommandBuilder(child("print('ok')")),
        controller,
    )

    async def scenario() -> None:
        descriptor_directory = Path("/proc/self/fd")
        descriptors_before = len(tuple(descriptor_directory.iterdir()))
        for _ in range(16):
            result = await supervisor.run(
                sandbox_profile,
                environment(sandbox_profile),
                cancellation=asyncio.Event(),
            )
            assert result.return_code == 0
        descriptors_after = len(tuple(descriptor_directory.iterdir()))
        assert descriptors_after <= descriptors_before + 1

    asyncio.run(scenario())
    assert len(controller.process_ids) == 16
    assert all(lease.killed and lease.closed for lease in controller.leases)
