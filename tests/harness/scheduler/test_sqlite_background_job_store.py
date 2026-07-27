"""Durable background-job ownership, transitions, recovery, and cleanup."""

import asyncio
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import (
    BackgroundJobStoreConflict,
    BackgroundJobStoreConflictCode,
    SQLiteBackgroundJobStore,
)
from app.services.harness.journal.sqlite_schema import SQLITE_SCHEMA
from app.services.harness.scheduler import BackgroundJobState
from tests.harness.operations.fixtures import (
    NOW,
    OPERATION_ID,
    WORKSPACE_ID,
)
from tests.harness.scheduler.fixtures import (
    EXECUTION_OWNER_ID,
    OTHER_OWNER_ID,
    OWNER_ID,
    database_path,
    queued_job,
    result_artifact,
    save_dispatched_operation,
    transition,
)


def test_restart_preserves_fenced_job_and_owner_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await save_dispatched_operation(path)
        store = await SQLiteBackgroundJobStore.open(path)
        queued = await store.save(queued_job())
        running = transition(
            queued,
            state=BackgroundJobState.RUNNING,
            execution_generation=1,
            execution_owner_id=EXECUTION_OWNER_ID,
            execution_lease_expires_at=NOW + timedelta(seconds=33),
            started_at=NOW + timedelta(seconds=3),
            updated_at=NOW + timedelta(seconds=3),
        )
        await store.save(running)
        await store.close()

        restarted = await SQLiteBackgroundJobStore.open(path)
        loaded = await restarted.load(WORKSPACE_ID, OWNER_ID, OPERATION_ID)
        recoverable = await restarted.load_recoverable()
        with pytest.raises(BackgroundJobStoreConflict) as denied:
            await restarted.load(WORKSPACE_ID, OTHER_OWNER_ID, OPERATION_ID)
        await restarted.close()

        assert loaded == running
        assert recoverable == (running,)
        assert denied.value.code is BackgroundJobStoreConflictCode.OWNER

    asyncio.run(scenario())


def test_terminal_job_is_retained_then_purged_in_bounded_batch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await save_dispatched_operation(path)
        store = await SQLiteBackgroundJobStore.open(path)
        queued = await store.save(queued_job())
        running = transition(
            queued,
            state=BackgroundJobState.RUNNING,
            execution_generation=1,
            execution_owner_id=EXECUTION_OWNER_ID,
            execution_lease_expires_at=NOW + timedelta(seconds=33),
            started_at=NOW + timedelta(seconds=3),
            updated_at=NOW + timedelta(seconds=3),
        )
        await store.save(running)
        terminal_at = NOW + timedelta(seconds=4)
        completed = transition(
            running,
            state=BackgroundJobState.COMPLETED,
            execution_owner_id=None,
            execution_lease_expires_at=None,
            updated_at=terminal_at,
            terminal_at=terminal_at,
            retention_expires_at=terminal_at + timedelta(minutes=1),
            result_artifact=result_artifact(),
        )
        await store.save(completed)

        before_expiry = await store.purge_expired(
            expired_at_or_before=terminal_at,
            maximum_records=1,
        )
        after_expiry = await store.purge_expired(
            expired_at_or_before=terminal_at + timedelta(minutes=1),
            maximum_records=1,
        )
        loaded = await store.load(WORKSPACE_ID, OWNER_ID, OPERATION_ID)
        await store.close()

        assert before_expiry == 0
        assert after_expiry == 1
        assert loaded is None

    asyncio.run(scenario())


def test_runtime_owner_cannot_be_stolen_during_lease_refresh(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await save_dispatched_operation(path)
        store = await SQLiteBackgroundJobStore.open(path)
        queued = await store.save(queued_job())
        running = transition(
            queued,
            state=BackgroundJobState.RUNNING,
            execution_generation=1,
            execution_owner_id=EXECUTION_OWNER_ID,
            execution_lease_expires_at=NOW + timedelta(seconds=33),
            started_at=NOW + timedelta(seconds=3),
            updated_at=NOW + timedelta(seconds=3),
        )
        await store.save(running)
        refreshed = transition(
            running,
            execution_lease_expires_at=NOW + timedelta(seconds=40),
            updated_at=NOW + timedelta(seconds=4),
        )
        assert await store.save(refreshed) == refreshed

        stolen = transition(
            refreshed,
            execution_owner_id="jow_" + "c" * 32,
            execution_lease_expires_at=NOW + timedelta(seconds=45),
            updated_at=NOW + timedelta(seconds=5),
        )
        with pytest.raises(BackgroundJobStoreConflict) as denied:
            await store.save(stolen)
        await store.close()

        assert denied.value.code is BackgroundJobStoreConflictCode.FENCING

    asyncio.run(scenario())


def test_v10_database_migrates_background_job_schema(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SQLITE_SCHEMA)
            connection.execute("DROP TABLE harness_background_jobs")
            connection.execute(
                """
                UPDATE harness_journal_schema
                SET schema_version = 10
                WHERE singleton = 1
                """
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(path, 0o600)

        store = await SQLiteBackgroundJobStore.open(path)
        await store.close()

        migrated = sqlite3.connect(path)
        try:
            version = migrated.execute(
                "SELECT schema_version FROM harness_journal_schema"
            ).fetchone()
            table = migrated.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'harness_background_jobs'
                """
            ).fetchone()
        finally:
            migrated.close()

        assert version == (11,)
        assert table == ("harness_background_jobs",)

    asyncio.run(scenario())
