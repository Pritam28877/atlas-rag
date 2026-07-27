"""Bounded startup resume and interrupted-job reconciliation."""

import asyncio
from datetime import timedelta
from pathlib import Path

from app.services.harness.journal import (
    SQLiteBackgroundJobStore,
    SQLiteRecoveryStore,
)
from app.services.harness.protocol import OperationState
from app.services.harness.protocol.background_jobs import (
    BackgroundJobConfiguration,
    BackgroundJobState,
)
from app.services.harness.recovery import BackgroundJobStartupRecovery
from tests.harness.operations.fixtures import NOW, OPERATION_ID, WORKSPACE_ID
from tests.harness.scheduler.fixtures import (
    EXECUTION_OWNER_ID,
    OWNER_ID,
    database_path,
    queued_job,
    save_dispatched_operation,
    transition,
)


def test_unstarted_durable_job_is_resumable(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await save_dispatched_operation(path)
        job_store = await SQLiteBackgroundJobStore.open(path)
        operation_store = await SQLiteRecoveryStore.open(path)
        queued = await job_store.save(queued_job())
        recovery = BackgroundJobStartupRecovery(
            job_store,
            operation_store,
            BackgroundJobConfiguration(maximum_queued_jobs=1),
        )

        report = await recovery.recover(
            recovered_at=NOW + timedelta(seconds=3),
            maximum_records=1,
        )
        await operation_store.close()
        await job_store.close()

        assert report.resumable_jobs == (queued,)
        assert report.terminalized_jobs == 0

    asyncio.run(scenario())


def test_expired_active_job_and_dispatched_operation_become_ambiguous(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await save_dispatched_operation(path)
        job_store = await SQLiteBackgroundJobStore.open(path)
        operation_store = await SQLiteRecoveryStore.open(path)
        queued = await job_store.save(queued_job())
        running = transition(
            queued,
            state=BackgroundJobState.RUNNING,
            execution_generation=1,
            execution_owner_id=EXECUTION_OWNER_ID,
            execution_lease_expires_at=NOW + timedelta(seconds=4),
            started_at=NOW + timedelta(seconds=3),
            updated_at=NOW + timedelta(seconds=3),
        )
        await job_store.save(running)
        recovery = BackgroundJobStartupRecovery(
            job_store,
            operation_store,
            BackgroundJobConfiguration(maximum_queued_jobs=1),
        )
        report = await recovery.recover(
            recovered_at=NOW + timedelta(seconds=5),
            maximum_records=1,
        )
        job = await job_store.load(WORKSPACE_ID, OWNER_ID, OPERATION_ID)
        operation = await operation_store.load_operation(
            WORKSPACE_ID,
            OPERATION_ID,
        )
        await operation_store.close()
        await job_store.close()

        assert report.terminalized_jobs == 1
        assert job is not None
        assert job.state is BackgroundJobState.AMBIGUOUS
        assert operation is not None
        assert operation.state is OperationState.AMBIGUOUS

    asyncio.run(scenario())
