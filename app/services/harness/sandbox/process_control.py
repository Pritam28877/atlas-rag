"""Bounded subprocess streams and mandatory process-tree cleanup."""

import asyncio
import os
import signal
from typing import Protocol

from app.services.harness.sandbox.errors import (
    SandboxOutputLimitExceeded,
    SandboxResourceCleanupError,
)

TERMINATION_GRACE_SECONDS = 0.5


class KillableResourceLease(Protocol):
    async def kill(self) -> None: ...


async def read_bounded(
    stream: asyncio.StreamReader,
    maximum_bytes: int,
    shared_total: list[int],
) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        shared_total[0] += len(chunk)
        if shared_total[0] > maximum_bytes:
            raise SandboxOutputLimitExceeded(
                "sandbox output exceeded its combined limit"
            )
        chunks.append(chunk)


async def write_nonblocking(
    descriptor: int,
    value: memoryview,
) -> None:
    loop = asyncio.get_running_loop()
    written = 0
    while written < len(value):
        try:
            written += os.write(descriptor, value[written:])
        except BlockingIOError:
            writable = loop.create_future()

            def mark_writable() -> None:
                if not writable.done():
                    writable.set_result(None)

            loop.add_writer(descriptor, mark_writable)
            try:
                await writable
            finally:
                loop.remove_writer(descriptor)


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    lease: KillableResourceLease | None,
) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=TERMINATION_GRACE_SECONDS,
            )
        except TimeoutError:
            pass
    lease_error: BaseException | None = None
    if lease is not None:
        try:
            await lease.kill()
        except BaseException as error:
            lease_error = error
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        await process.wait()
    if lease_error is not None:
        raise SandboxResourceCleanupError(
            "sandbox resource cleanup failed"
        ) from lease_error
