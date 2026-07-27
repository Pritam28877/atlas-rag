"""Background-job result limits fail closed before artifact persistence."""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from app.services.harness.artifacts import (
    BlobErrorCode,
    BlobStoreError,
    LocalBackgroundJobArtifactWriter,
    LocalBlobStore,
)
from app.services.harness.journal import SQLiteBackgroundJobStore
from app.services.harness.protocol import OperationRecord, OperationState
from app.services.harness.protocol.background_jobs import (
    BackgroundJobConfiguration,
    BackgroundJobRecord,
    BackgroundJobState,
)
from app.services.harness.scheduler import (
    BackgroundJobCoordinator,
    BackgroundJobExecutionResult,
)
from tests.harness.operations.fixtures import NOW, OPERATION_ID, WORKSPACE_ID
from tests.harness.scheduler.fixtures import (
    EXECUTION_OWNER_ID,
    OWNER_ID,
    FixedClock,
    database_path,
    persist_terminal_operation,
    queued_job,
    save_dispatched_operation,
    terminal_operation,
)


class OversizedExecutor:
    def __init__(self, database: Path, operation: OperationRecord) -> None:
        self._database = database
        self._operation = operation

    async def execute(
        self,
        job: BackgroundJobRecord,
        *,
        cancellation: asyncio.Event,
    ) -> BackgroundJobExecutionResult:
        content = b"12345"
        terminal = terminal_operation(
            self._operation,
            OperationState.COMPLETED,
            result_content=content,
        )
        await persist_terminal_operation(self._database, terminal)
        return BackgroundJobExecutionResult(
            status=BackgroundJobState.COMPLETED,
            operation=terminal,
            result_bytes=content,
        )


async def wait_for_terminal(
    coordinator: BackgroundJobCoordinator,
) -> BackgroundJobRecord:
    async with asyncio.timeout(2):
        while True:
            job = await coordinator.status(
                WORKSPACE_ID,
                OWNER_ID,
                OPERATION_ID,
            )
            assert job is not None
            if job.state is BackgroundJobState.FAILED:
                return job
            await asyncio.sleep(0.001)


def test_oversized_result_is_not_written_to_artifact_store(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        operation = await save_dispatched_operation(path)
        store = await SQLiteBackgroundJobStore.open(path)
        storage_root = tmp_path / "bounded-artifacts"
        storage_root.mkdir(mode=0o700)
        os.chmod(storage_root, 0o700)
        blob_store = await LocalBlobStore.open(storage_root, WORKSPACE_ID)
        coordinator = BackgroundJobCoordinator(
            store,
            OversizedExecutor(path, operation),
            LocalBackgroundJobArtifactWriter(blob_store),
            execution_owner_id=EXECUTION_OWNER_ID,
            configuration=BackgroundJobConfiguration(
                maximum_concurrent_jobs=1,
                maximum_queued_jobs=1,
                maximum_result_bytes=4,
                maximum_log_bytes=4,
            ),
            clock=FixedClock(NOW.replace(hour=14, second=2)),
        )
        await coordinator.start()
        await coordinator.submit(queued_job())
        terminal = await wait_for_terminal(coordinator)
        await coordinator.close()
        with pytest.raises(BlobStoreError) as missing:
            await blob_store.inspect(hashlib.sha256(b"12345").hexdigest())
        await blob_store.close()
        await store.close()

        assert terminal.result_artifact is None
        assert missing.value.code is BlobErrorCode.NOT_FOUND

    asyncio.run(scenario())
