"""Async owner for one bounded sandbox process tree."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.harness.sandbox.environment import (
    SandboxChildEnvironment,
    SandboxEnvironmentError,
    prepare_child_environment,
)
from app.services.harness.sandbox.errors import (
    SandboxOutputLimitExceeded,
    SandboxProcessCancelled,
    SandboxProcessTimedOut,
    SandboxSupervisorError,
)
from app.services.harness.sandbox.models import (
    SandboxProfile,
    SandboxResourceLimits,
)
from app.services.harness.sandbox.process_control import (
    read_bounded,
    terminate_process_tree,
    write_nonblocking,
)


@dataclass(frozen=True, slots=True)
class SandboxProcessResult:
    return_code: int
    stdout: bytes
    stderr: bytes


class SandboxCommandBuilder(Protocol):
    def build(
        self,
        profile: SandboxProfile,
        *,
        admission_fifo: Path,
        secret_fifo: Path,
        parent_environment: Mapping[str, str],
    ) -> list[str]: ...


class SandboxResourceLease(Protocol):
    async def kill(self) -> None: ...

    async def close(self) -> None: ...


class SandboxResourceController(Protocol):
    async def admit(
        self,
        process_id: int,
        limits: SandboxResourceLimits,
    ) -> SandboxResourceLease: ...


class SandboxSupervisor:
    def __init__(
        self,
        command_builder: SandboxCommandBuilder,
        resource_controller: SandboxResourceController,
    ) -> None:
        self._command_builder = command_builder
        self._resource_controller = resource_controller

    async def run(
        self,
        profile: SandboxProfile,
        environment: SandboxChildEnvironment,
        *,
        cancellation: asyncio.Event,
    ) -> SandboxProcessResult:
        if cancellation.is_set():
            raise SandboxProcessCancelled("sandbox execution was cancelled")
        try:
            parent_environment, secret_packet = prepare_child_environment(
                profile,
                environment,
            )
        except SandboxEnvironmentError as error:
            raise SandboxSupervisorError(str(error)) from None
        deadline = (
            asyncio.get_running_loop().time()
            + profile.limits.wall_time_ms / 1000
        )
        process: asyncio.subprocess.Process | None = None
        lease: SandboxResourceLease | None = None
        admission_fd: int | None = None
        secret_fd: int | None = None
        cancellation_task = asyncio.create_task(cancellation.wait())
        output_tasks: (
            tuple[
                asyncio.Task[bytes],
                asyncio.Task[bytes],
                asyncio.Task[int],
            ]
            | None
        ) = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="atlas-sandbox-",
            ) as temporary_directory:
                temporary = Path(temporary_directory)
                admission_fifo = temporary / "admission"
                secret_fifo = temporary / "secrets"
                os.mkfifo(admission_fifo, mode=0o600)
                os.mkfifo(secret_fifo, mode=0o600)
                admission_fd = os.open(
                    admission_fifo,
                    os.O_RDWR | os.O_NONBLOCK,
                )
                secret_fd = os.open(secret_fifo, os.O_RDWR | os.O_NONBLOCK)
                command = self._command_builder.build(
                    profile,
                    admission_fifo=admission_fifo,
                    secret_fifo=secret_fifo,
                    parent_environment=parent_environment,
                )
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={"PATH": "/usr/bin:/bin"},
                    start_new_session=True,
                )
                if process.stdout is None or process.stderr is None:
                    raise SandboxSupervisorError(
                        "sandbox output pipes were not created"
                    )
                shared_output_bytes = [0]
                stdout_task = asyncio.create_task(
                    read_bounded(
                        process.stdout,
                        profile.limits.output_bytes,
                        shared_output_bytes,
                    )
                )
                stderr_task = asyncio.create_task(
                    read_bounded(
                        process.stderr,
                        profile.limits.output_bytes,
                        shared_output_bytes,
                    )
                )
                wait_task = asyncio.create_task(process.wait())
                output_tasks = (stdout_task, stderr_task, wait_task)

                lease = await _admit_or_cancel(
                    self._resource_controller,
                    process.pid,
                    profile.limits,
                    cancellation_task,
                    deadline,
                )
                await _complete_or_cancel(
                    write_nonblocking(admission_fd, memoryview(b"1")),
                    cancellation_task,
                    deadline,
                )
                await _complete_or_cancel(
                    write_nonblocking(
                        secret_fd,
                        memoryview(secret_packet),
                    ),
                    cancellation_task,
                    deadline,
                )

                completion = asyncio.gather(*output_tasks)
                done, _ = await asyncio.wait(
                    (completion, cancellation_task),
                    timeout=_remaining_seconds(deadline),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation_task in done and cancellation_task.result():
                    raise SandboxProcessCancelled(
                        "sandbox execution was cancelled"
                    )
                if completion not in done:
                    raise SandboxProcessTimedOut(
                        "sandbox execution exceeded its wall-time limit"
                    )
                stdout_value, stderr_value, return_code_value = await completion
                if (
                    not isinstance(stdout_value, bytes)
                    or not isinstance(stderr_value, bytes)
                    or not isinstance(return_code_value, int)
                ):
                    raise SandboxSupervisorError(
                        "sandbox result types are invalid"
                    )
                return SandboxProcessResult(
                    return_code=return_code_value,
                    stdout=stdout_value,
                    stderr=stderr_value,
                )
        except asyncio.CancelledError:
            raise SandboxProcessCancelled("sandbox execution was cancelled") from None
        except (
            SandboxOutputLimitExceeded,
            SandboxProcessCancelled,
            SandboxProcessTimedOut,
            SandboxSupervisorError,
        ):
            raise
        except BaseException:
            raise SandboxSupervisorError("sandbox execution failed") from None
        finally:
            secret_packet[:] = bytes(len(secret_packet))
            for descriptor in (admission_fd, secret_fd):
                if descriptor is not None:
                    os.close(descriptor)
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)
            await _cleanup_process_tree(process, lease, output_tasks)


async def _admit_or_cancel(
    controller: SandboxResourceController,
    process_id: int,
    limits: SandboxResourceLimits,
    cancellation_task: asyncio.Task[bool],
    deadline: float,
) -> SandboxResourceLease:
    admission_task = asyncio.create_task(controller.admit(process_id, limits))
    done, _ = await asyncio.wait(
        (admission_task, cancellation_task),
        timeout=_remaining_seconds(deadline),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if cancellation_task in done and cancellation_task.result():
        admission_task.cancel()
        await asyncio.gather(admission_task, return_exceptions=True)
        raise SandboxProcessCancelled("sandbox execution was cancelled")
    if admission_task not in done:
        admission_task.cancel()
        await asyncio.gather(admission_task, return_exceptions=True)
        raise SandboxProcessTimedOut(
            "sandbox resource admission exceeded its wall-time limit"
        )
    try:
        return admission_task.result()
    except BaseException:
        raise SandboxSupervisorError("sandbox resource admission failed") from None


async def _cleanup_process_tree(
    process: asyncio.subprocess.Process | None,
    lease: SandboxResourceLease | None,
    output_tasks: (
        tuple[
            asyncio.Task[bytes],
            asyncio.Task[bytes],
            asyncio.Task[int],
        ]
        | None
    ),
) -> None:
    cleanup_errors: list[BaseException] = []
    if process is not None:
        try:
            await terminate_process_tree(process, lease)
        except BaseException as error:
            cleanup_errors.append(error)
    if output_tasks is not None:
        await asyncio.gather(*output_tasks, return_exceptions=True)
    if lease is not None:
        try:
            await lease.close()
        except BaseException as error:
            cleanup_errors.append(error)
    if cleanup_errors:
        raise SandboxSupervisorError(
            "sandbox process cleanup failed"
        ) from cleanup_errors[0]


def _remaining_seconds(deadline: float) -> float:
    return max(0, deadline - asyncio.get_running_loop().time())


async def _complete_or_cancel(
    awaitable: Awaitable[None],
    cancellation_task: asyncio.Task[bool],
    deadline: float,
) -> None:
    operation_task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait(
        (operation_task, cancellation_task),
        timeout=_remaining_seconds(deadline),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if cancellation_task in done and cancellation_task.result():
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise SandboxProcessCancelled("sandbox execution was cancelled")
    if operation_task not in done:
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise SandboxProcessTimedOut(
            "sandbox environment delivery exceeded its wall-time limit"
        )
    await operation_task
