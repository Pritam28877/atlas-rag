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
    SQLiteRecoveryStore,
)
from app.services.harness.journal.sqlite_schema import SQLITE_SCHEMA
from app.services.harness.scheduler import (
    BackgroundJobArtifact,
    BackgroundJobRecord,
    BackgroundJobState,
)
from app.services.harness.tools.operation_lifecycle import (
    DurableOperationLifecycle,
)
from tests.harness.operations.fixtures import (
    ARGS_SHA256,
    NOW,
    OPERATION_ID,
    WORKSPACE_ID,
    fence,
    request,
)

OWNER_ID = "prn_" + "7" * 32
OTHER_OWNER_ID = "prn_" + "8" * 32
EXECUTION_OWNER_ID = "jow_" + "9" * 32


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def queued_job() -> BackgroundJobRecord:
    return BackgroundJobRecord(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        owner_principal_id=OWNER_ID,
        call_id="background-call-1",
        tool_name="workspace.write_file",
        tool_version="1.0.0",
        descriptor_sha256="a" * 64,
        args_sha256=ARGS_SHA256,
        state=BackgroundJobState.QUEUED,
        created_at=NOW + timedelta(seconds=2),
        updated_at=NOW + timedelta(seconds=2),
        queue_expires_at=NOW + timedelta(hours=1),
    )


def transition(
    job: BackgroundJobRecord,
    **changes: object,
) -> BackgroundJobRecord:
    values = job.model_dump(mode="python")
    values.update(changes)
    return BackgroundJobRecord.model_validate(values)


def result_artifact() -> BackgroundJobArtifact:
    return BackgroundJobArtifact(
        artifact_id="art_" + "a" * 32,
        media_type="application/json",
        size_bytes=24,
        content_sha256="b" * 64,
    )


async def save_dispatched_operation(path: Path) -> None:
    recovery_store = await SQLiteRecoveryStore.open(path)
    lifecycle = DurableOperationLifecycle(recovery_store)
    prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)
    await lifecycle.dispatch(
        prepared,
        fence(),
        dispatched_at=NOW + timedelta(seconds=1),
    )
    await recovery_store.close()


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
