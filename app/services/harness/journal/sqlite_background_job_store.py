"""Bounded owner for durable background-job persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.services.harness.journal.errors import (
    BackgroundJobStoreConflict,
    BackgroundJobStoreConflictCode,
)
from app.services.harness.journal.sqlite_background_job_operations import (
    purge_expired_background_jobs,
    save_background_job,
)
from app.services.harness.journal.sqlite_background_job_rows import (
    decode_background_job,
    load_background_job_row,
    load_owned_background_jobs,
    load_recoverable_background_jobs,
)
from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.protocol import OperationId, PrincipalId, WorkspaceId
from app.services.harness.protocol.background_jobs import (
    MAXIMUM_BACKGROUND_JOB_PAGE,
    MAXIMUM_BACKGROUND_JOBS,
    BackgroundJobRecord,
)

MAXIMUM_BACKGROUND_JOB_PURGE_BATCH = 256


class SQLiteBackgroundJobStore:
    def __init__(
        self,
        connection_owner: SQLiteConnectionOwner,
        *,
        maximum_jobs_per_workspace: int,
    ) -> None:
        self._connection_owner = connection_owner
        self._maximum_jobs_per_workspace = maximum_jobs_per_workspace

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 32,
        maximum_jobs_per_workspace: int = 4_096,
    ) -> SQLiteBackgroundJobStore:
        if not 1 <= maximum_jobs_per_workspace <= MAXIMUM_BACKGROUND_JOBS:
            raise ValueError(
                "background job capacity must be between 1 and 4096"
            )
        owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await owner.initialize()
        return cls(
            owner,
            maximum_jobs_per_workspace=maximum_jobs_per_workspace,
        )

    async def close(self) -> None:
        await self._connection_owner.close()

    async def save(self, job: BackgroundJobRecord) -> BackgroundJobRecord:
        return await self._connection_owner.execute(
            lambda connection: save_background_job(
                connection,
                job,
                maximum_jobs_per_workspace=self._maximum_jobs_per_workspace,
            )
        )

    async def load(
        self,
        workspace_id: WorkspaceId,
        owner_principal_id: PrincipalId,
        operation_id: OperationId,
    ) -> BackgroundJobRecord | None:
        job = await self._connection_owner.execute(
            lambda connection: self._load(
                connection,
                workspace_id,
                operation_id,
            )
        )
        if job is not None and job.owner_principal_id != owner_principal_id:
            raise BackgroundJobStoreConflict(
                BackgroundJobStoreConflictCode.OWNER
            )
        return job

    async def load_owned(
        self,
        workspace_id: WorkspaceId,
        owner_principal_id: PrincipalId,
        *,
        maximum_records: int = 200,
    ) -> tuple[BackgroundJobRecord, ...]:
        if not 1 <= maximum_records <= MAXIMUM_BACKGROUND_JOB_PAGE:
            raise ValueError("background job page must be between 1 and 200")
        return await self._connection_owner.execute(
            lambda connection: load_owned_background_jobs(
                connection,
                workspace_id,
                owner_principal_id,
                maximum_records=maximum_records,
            )
        )

    async def load_recoverable(
        self,
        *,
        maximum_records: int = 256,
    ) -> tuple[BackgroundJobRecord, ...]:
        return await self._connection_owner.execute(
            lambda connection: load_recoverable_background_jobs(
                connection,
                maximum_records=maximum_records,
            )
        )

    async def purge_expired(
        self,
        *,
        expired_at_or_before: datetime,
        maximum_records: int = 128,
    ) -> int:
        self._require_utc(expired_at_or_before)
        if not 1 <= maximum_records <= MAXIMUM_BACKGROUND_JOB_PURGE_BATCH:
            raise ValueError("background job purge batch must be between 1 and 256")
        return await self._connection_owner.execute(
            lambda connection: purge_expired_background_jobs(
                connection,
                expired_at_or_before=expired_at_or_before,
                maximum_records=maximum_records,
            )
        )

    @staticmethod
    def _load(
        connection: sqlite3.Connection,
        workspace_id: str,
        operation_id: str,
    ) -> BackgroundJobRecord | None:
        row = load_background_job_row(connection, workspace_id, operation_id)
        return decode_background_job(row) if row is not None else None

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("background job timestamp must use UTC")
