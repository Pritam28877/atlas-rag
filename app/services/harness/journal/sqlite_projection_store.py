"""Off-event-loop SQLite CAS store for projection checkpoints."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services.harness.journal.projection_contracts import (
    ProjectionCheckpoint,
    ProjectionName,
)
from app.services.harness.journal.projection_store import (
    FailureCode,
    ProjectionExpectation,
    ProjectionHealth,
    ProjectionStoreConflict,
    StoredProjection,
)
from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.journal.sqlite_projection_rows import (
    stored_projection_from_row,
)
from app.services.harness.journal.sqlite_projection_sql import (
    ADVANCE_PROJECTION,
    INSERT_PROJECTION,
    LOAD_PROJECTION,
    MARK_PROJECTION_UNHEALTHY,
    REBUILD_PROJECTION,
)
from app.services.harness.protocol import WorkspaceId


class SQLiteProjectionStore:
    def __init__(
        self,
        connection_owner: SQLiteConnectionOwner,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._connection_owner = connection_owner
        self._clock = clock

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 64,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> SQLiteProjectionStore:
        connection_owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await connection_owner.initialize()
        return cls(connection_owner, clock=clock)

    async def close(self) -> None:
        await self._connection_owner.close()

    async def load(
        self,
        workspace_id: WorkspaceId,
        projection_name: ProjectionName,
    ) -> StoredProjection | None:
        return await self._connection_owner.execute(
            lambda connection: self._load(
                connection,
                workspace_id,
                projection_name,
            )
        )

    async def save_online(
        self,
        checkpoint: ProjectionCheckpoint,
        expected: ProjectionExpectation,
    ) -> StoredProjection:
        statement = (
            INSERT_PROJECTION
            if expected.generation is None
            else ADVANCE_PROJECTION
        )
        return await self._connection_owner.execute(
            lambda connection: self._write(
                connection,
                checkpoint,
                expected,
                statement,
            )
        )

    async def replace_rebuild(
        self,
        checkpoint: ProjectionCheckpoint,
        expected: ProjectionExpectation,
    ) -> StoredProjection:
        statement = (
            INSERT_PROJECTION
            if expected.generation is None
            else REBUILD_PROJECTION
        )
        return await self._connection_owner.execute(
            lambda connection: self._write(
                connection,
                checkpoint,
                expected,
                statement,
            )
        )

    async def mark_unhealthy(
        self,
        workspace_id: WorkspaceId,
        projection_name: ProjectionName,
        health: ProjectionHealth,
        failure_code: FailureCode,
        expected: ProjectionExpectation,
    ) -> StoredProjection:
        if health is ProjectionHealth.HEALTHY:
            raise ValueError("mark_unhealthy requires an unhealthy status")
        if expected.generation is None or expected.last_journal_sequence is None:
            raise ProjectionStoreConflict
        parameters: dict[str, object] = {
            "workspace_id": workspace_id,
            "projection_name": projection_name,
            "projection_status": health.value,
            "failure_code": failure_code,
            "expected_generation": expected.generation,
            "expected_sequence": expected.last_journal_sequence,
            "updated_at": self._utc_now().isoformat(),
        }
        return await self._connection_owner.execute(
            lambda connection: self._execute_write(
                connection,
                MARK_PROJECTION_UNHEALTHY,
                parameters,
            )
        )

    @staticmethod
    def _load(
        connection: sqlite3.Connection,
        workspace_id: str,
        projection_name: str,
    ) -> StoredProjection | None:
        row = connection.execute(
            LOAD_PROJECTION,
            {
                "workspace_id": workspace_id,
                "projection_name": projection_name,
            },
        ).fetchone()
        return None if row is None else stored_projection_from_row(row)

    def _write(
        self,
        connection: sqlite3.Connection,
        checkpoint: ProjectionCheckpoint,
        expected: ProjectionExpectation,
        statement: str,
    ) -> StoredProjection:
        parameters: dict[str, object] = {
            "workspace_id": checkpoint.workspace_id,
            "projection_name": checkpoint.projection_name,
            "projection_version": checkpoint.projection_version,
            "last_journal_sequence": checkpoint.last_journal_sequence,
            "event_count": checkpoint.event_count,
            "state_json": checkpoint.state_json,
            "state_sha256": checkpoint.state_sha256,
            "updated_at": self._utc_now().isoformat(),
        }
        if expected.generation is not None:
            parameters["expected_generation"] = expected.generation
            parameters["expected_sequence"] = expected.last_journal_sequence
        return self._execute_write(connection, statement, parameters)

    @staticmethod
    def _execute_write(
        connection: sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> StoredProjection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(statement, parameters).fetchone()
            if row is None:
                raise ProjectionStoreConflict
            stored = stored_projection_from_row(row)
            connection.commit()
            return stored
        except BaseException:
            connection.rollback()
            raise

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("projection clock must return UTC")
        return value
