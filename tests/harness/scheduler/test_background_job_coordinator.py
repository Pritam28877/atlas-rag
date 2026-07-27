"""Real-store background execution, artifacts, cancellation, and bounds."""

import asyncio
import os
from pathlib import Path

from app.services.harness.artifacts import (
    BlobRangeRequest,
    LocalBackgroundJobArtifactWriter,
    LocalBlobStore,
)
from app.services.harness.journal import SQLiteBackgroundJobStore
from app.services.harness.protocol import OperationRecord, OperationState
from app.services.harness.protocol.background_jobs import (
    TERMINAL_BACKGROUND_JOB_STATES,
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


class ImmediateExecutor:
    def __init__(self, database: Path, operation: OperationRecord) -> None:
        self._database = database
        self._operation = operation

    async def execute(
        self,
        job: BackgroundJobRecord,
        *,
        cancellation: asyncio.Event,
    ) -> BackgroundJobExecutionResult:
        assert job.operation_id == OPERATION_ID
        assert not cancellation.is_set()
        content = b'{"ok":true}'
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
            log_bytes=b"completed\n",
        )


class CancellingExecutor:
    def __init__(self, database: Path, operation: OperationRecord) -> None:
        self._database = database
        self._operation = operation
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def execute(
        self,
        job: BackgroundJobRecord,
        *,
        cancellation: asyncio.Event,
    ) -> BackgroundJobExecutionResult:
        self.started.set()
        try:
            await cancellation.wait()
            terminal = terminal_operation(
                self._operation,
                OperationState.CANCELLED,
                status_reason="cancelled",
            )
            await persist_terminal_operation(self._database, terminal)
            return BackgroundJobExecutionResult(
                status=BackgroundJobState.CANCELLED,
                operation=terminal,
            )
        finally:
            self.stopped.set()
async def open_blob_store(
    tmp_path: Path,
) -> LocalBlobStore:
    storage_root = tmp_path / "background-artifacts"
    storage_root.mkdir(mode=0o700)
    os.chmod(storage_root, 0o700)
    return await LocalBlobStore.open(storage_root, WORKSPACE_ID)


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
            if job.state in TERMINAL_BACKGROUND_JOB_STATES:
                return job
            await asyncio.sleep(0.001)


def test_job_completes_with_durable_content_addressed_artifacts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        operation = await save_dispatched_operation(path)
        store = await SQLiteBackgroundJobStore.open(path)
        blob_store = await open_blob_store(tmp_path)
        coordinator = BackgroundJobCoordinator(
            store,
            ImmediateExecutor(path, operation),
            LocalBackgroundJobArtifactWriter(blob_store),
            execution_owner_id=EXECUTION_OWNER_ID,
            configuration=BackgroundJobConfiguration(
                maximum_concurrent_jobs=1,
                maximum_queued_jobs=1,
            ),
            clock=FixedClock(NOW.replace(hour=14, second=2)),
        )
        await coordinator.start()
        await coordinator.submit(queued_job())
        completed = await wait_for_terminal(coordinator)

        assert completed.state is BackgroundJobState.COMPLETED
        assert completed.result_artifact is not None
        assert completed.log_artifact is not None
        result_metadata = await blob_store.inspect(
            completed.result_artifact.content_sha256
        )
        chunks = [
            chunk
            async for chunk in blob_store.read_range(
                BlobRangeRequest(
                    content_sha256=completed.result_artifact.content_sha256,
                    length_bytes=result_metadata.size_bytes,
                )
            )
        ]
        await coordinator.close()
        await blob_store.close()
        await store.close()

        assert b"".join(chunks) == b'{"ok":true}'

    asyncio.run(scenario())


def test_owner_cancel_stops_active_execution_and_persists_terminal_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        operation = await save_dispatched_operation(path)
        store = await SQLiteBackgroundJobStore.open(path)
        blob_store = await open_blob_store(tmp_path)
        executor = CancellingExecutor(path, operation)
        coordinator = BackgroundJobCoordinator(
            store,
            executor,
            LocalBackgroundJobArtifactWriter(blob_store),
            execution_owner_id=EXECUTION_OWNER_ID,
            configuration=BackgroundJobConfiguration(
                maximum_concurrent_jobs=1,
                maximum_queued_jobs=1,
            ),
            clock=FixedClock(NOW.replace(hour=14, second=2)),
        )
        await coordinator.start()
        await coordinator.submit(queued_job())
        await asyncio.wait_for(executor.started.wait(), timeout=2)
        cancelling = await coordinator.cancel(
            WORKSPACE_ID,
            OWNER_ID,
            OPERATION_ID,
        )
        terminal = await wait_for_terminal(coordinator)
        await asyncio.wait_for(executor.stopped.wait(), timeout=2)
        await coordinator.close()
        await blob_store.close()
        await store.close()

        assert cancelling is not None
        assert cancelling.state is BackgroundJobState.CANCELLING
        assert terminal.state is BackgroundJobState.CANCELLED
        assert terminal.execution_owner_id is None

    asyncio.run(scenario())
