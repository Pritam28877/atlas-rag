"""Bounded async owner for descriptor-relative workspace file operations."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol, TypeVar

from app.services.harness.tools.file_contracts import (
    ReadFileArguments,
    ReadFileResult,
    SearchFilesArguments,
    SearchFilesResult,
)
from app.services.harness.tools.workspace_descriptor import (
    WorkspaceDescriptor,
    WorkspaceFileError,
    WorkspaceFileErrorCode,
)

ResultType = TypeVar("ResultType")
MAXIMUM_FILE_OPERATION_TIMEOUT_MS = 60_000


class WorkspaceFileBackend(Protocol):
    def read(
        self,
        arguments: ReadFileArguments,
        *,
        stop: threading.Event,
        deadline: float,
    ) -> ReadFileResult: ...

    def search(
        self,
        arguments: SearchFilesArguments,
        *,
        stop: threading.Event,
        deadline: float,
    ) -> SearchFilesResult: ...

    def close(self) -> None: ...


class WorkspaceFileService:
    def __init__(
        self,
        files: WorkspaceFileBackend,
        executor: ThreadPoolExecutor,
        *,
        maximum_pending_operations: int,
    ) -> None:
        self._files = files
        self._executor = executor
        self._maximum_pending_operations = maximum_pending_operations
        self._pending_operations = 0
        self._closed = False

    @classmethod
    async def open(
        cls,
        workspace: Path,
        *,
        maximum_pending_operations: int = 64,
    ) -> WorkspaceFileService:
        if not 1 <= maximum_pending_operations <= 1024:
            raise ValueError("file operation capacity is outside bounds")
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="atlas-workspace-files",
        )
        loop = asyncio.get_running_loop()
        try:
            files = await loop.run_in_executor(
                executor,
                WorkspaceDescriptor,
                workspace,
            )
        except BaseException:
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        return cls(
            files,
            executor,
            maximum_pending_operations=maximum_pending_operations,
        )

    async def read(
        self,
        arguments: ReadFileArguments,
        *,
        cancellation: asyncio.Event,
        timeout_ms: int,
    ) -> ReadFileResult:
        return await self._execute(
            lambda stop, deadline: self._files.read(
                arguments,
                stop=stop,
                deadline=deadline,
            ),
            cancellation=cancellation,
            timeout_ms=timeout_ms,
        )

    async def search(
        self,
        arguments: SearchFilesArguments,
        *,
        cancellation: asyncio.Event,
        timeout_ms: int,
    ) -> SearchFilesResult:
        return await self._execute(
            lambda stop, deadline: self._files.search(
                arguments,
                stop=stop,
                deadline=deadline,
            ),
            cancellation=cancellation,
            timeout_ms=timeout_ms,
        )

    async def close(self) -> None:
        if self._closed:
            return
        if self._pending_operations:
            raise WorkspaceFileError(WorkspaceFileErrorCode.CAPACITY)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._files.close)
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def _execute(
        self,
        operation: Callable[
            [threading.Event, float],
            ResultType,
        ],
        *,
        cancellation: asyncio.Event,
        timeout_ms: int,
    ) -> ResultType:
        if self._closed:
            raise WorkspaceFileError(WorkspaceFileErrorCode.CLOSED)
        if not 1 <= timeout_ms <= MAXIMUM_FILE_OPERATION_TIMEOUT_MS:
            raise ValueError("file operation timeout is outside bounds")
        if cancellation.is_set():
            raise WorkspaceFileError(WorkspaceFileErrorCode.CANCELLED)
        if self._pending_operations >= self._maximum_pending_operations:
            raise WorkspaceFileError(WorkspaceFileErrorCode.CAPACITY)

        self._pending_operations += 1
        stop = threading.Event()
        deadline = time.monotonic() + timeout_ms / 1000
        loop = asyncio.get_running_loop()
        operation_future = loop.run_in_executor(
            self._executor,
            operation,
            stop,
            deadline,
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (operation_future, cancellation_task),
                timeout=timeout_ms / 1000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_future in done:
                try:
                    return operation_future.result()
                except WorkspaceFileError:
                    raise
                except Exception:
                    raise WorkspaceFileError(
                        WorkspaceFileErrorCode.IO
                    ) from None
            stop.set()
            try:
                await asyncio.shield(operation_future)
            except WorkspaceFileError:
                pass
            if cancellation_task in done and cancellation_task.result():
                raise WorkspaceFileError(
                    WorkspaceFileErrorCode.CANCELLED
                )
            raise WorkspaceFileError(WorkspaceFileErrorCode.TIMEOUT)
        except asyncio.CancelledError:
            stop.set()
            try:
                await asyncio.shield(operation_future)
            except Exception:
                pass
            raise
        finally:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)
            self._pending_operations -= 1
