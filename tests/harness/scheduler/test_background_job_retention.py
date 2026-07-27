"""Real SQLite exact-row expired-job cleanup."""

import asyncio
from datetime import timedelta
from pathlib import Path

from app.services.harness.journal import SQLiteBackgroundJobStore
from app.services.harness.protocol.background_jobs import BackgroundJobState
from app.services.harness.recovery import BackgroundJobRetentionCleaner
from tests.harness.operations.fixtures import NOW, OPERATION_ID, WORKSPACE_ID
from tests.harness.scheduler.fixtures import (
    EXECUTION_OWNER_ID,
    OWNER_ID,
    database_path,
    queued_job,
    result_artifact,
    save_dispatched_operation,
    transition,
)


def test_expired_terminal_job_is_deleted_in_bounded_batch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await save_dispatched_operation(path)
        job_store = await SQLiteBackgroundJobStore.open(path)
        queued = await job_store.save(queued_job())
        running = transition(
            queued,
            state=BackgroundJobState.RUNNING,
            execution_generation=1,
            execution_owner_id=EXECUTION_OWNER_ID,
            execution_lease_expires_at=NOW + timedelta(seconds=33),
            started_at=NOW + timedelta(seconds=3),
            updated_at=NOW + timedelta(seconds=3),
        )
        await job_store.save(running)
        artifact = result_artifact()
        terminal_at = NOW + timedelta(seconds=4)
        completed = transition(
            running,
            state=BackgroundJobState.COMPLETED,
            execution_owner_id=None,
            execution_lease_expires_at=None,
            updated_at=terminal_at,
            terminal_at=terminal_at,
            retention_expires_at=terminal_at + timedelta(minutes=1),
            result_artifact=artifact,
        )
        await job_store.save(completed)

        cleaner = BackgroundJobRetentionCleaner(job_store)
        report = await cleaner.clean(
            observed_at=terminal_at + timedelta(minutes=1),
            maximum_records=1,
        )
        remaining = await job_store.load(
            WORKSPACE_ID,
            OWNER_ID,
            OPERATION_ID,
        )
        await job_store.close()

        assert report.jobs_deleted == 1
        assert remaining is None

    asyncio.run(scenario())
