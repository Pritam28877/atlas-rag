"""Bounded worker-thread ownership for local artifact filesystem I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from app.services.harness.artifacts.contracts import (
    BlobErrorCode,
    BlobStoreError,
)
from app.services.harness.artifacts.local_files import LocalBlobFiles

ResultType = TypeVar("ResultType")


class LocalBlobIOOwner:
    def __init__(
        self,
        storage_root: Path,
        workspace_id: str,
        *,
        maximum_pending_operations: int,
    ) -> None:
        if not 1 <= maximum_pending_operations <= 1024:
            raise ValueError(
                "maximum pending blob operations must be between 1 and 1024"
            )
        self.files = LocalBlobFiles(storage_root, workspace_id)
        self._maximum_pending_operations = maximum_pending_operations
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="atlas-artifact-local",
        )
        self._pending_operations = 0
        self._closed = False

    async def initialize(self) -> None:
        try:
            await self.execute(self.files.initialize)
        except BaseException:
            await self.close()
            raise

    async def execute(
        self,
        operation: Callable[[], ResultType],
    ) -> ResultType:
        if self._closed:
            raise BlobStoreError(BlobErrorCode.CLOSED)
        if self._pending_operations >= self._maximum_pending_operations:
            raise BlobStoreError(BlobErrorCode.CAPACITY)
        self._pending_operations += 1
        operation_future = self._submit(operation)
        try:
            return await asyncio.shield(operation_future)
        except BlobStoreError:
            raise
        except OSError as error:
            raise BlobStoreError(BlobErrorCode.INVALID_STORAGE) from error
        finally:
            if operation_future.done():
                self._pending_operations -= 1
            else:
                operation_future.add_done_callback(self._release_operation)

    async def close(self) -> None:
        if self._closed:
            return
        if self._pending_operations:
            raise BlobStoreError(BlobErrorCode.CAPACITY)
        await self.execute(self.files.close)
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _submit(
        self,
        operation: Callable[[], ResultType],
    ) -> asyncio.Future[ResultType]:
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._executor, operation)

    def _release_operation(
        self,
        operation_future: asyncio.Future[ResultType],
    ) -> None:
        self._pending_operations -= 1
        if not operation_future.cancelled():
            operation_future.exception()
