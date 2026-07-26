"""Async PostgreSQL CAS store for deterministic projection checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.postgres_projection_rows import (
    stored_projection_from_row,
)
from app.services.harness.journal.postgres_projection_sql import (
    ADVANCE_PROJECTION,
    INSERT_PROJECTION,
    LOAD_PROJECTION,
    MARK_PROJECTION_UNHEALTHY,
    REBUILD_PROJECTION,
)
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
from app.services.harness.protocol import WorkspaceId

type TransactionFactory = Callable[
    [],
    AbstractAsyncContextManager[AsyncSession],
]


class PostgresProjectionStore:
    def __init__(self, transaction_factory: TransactionFactory) -> None:
        self._transaction_factory = transaction_factory

    async def load(
        self,
        workspace_id: WorkspaceId,
        projection_name: ProjectionName,
    ) -> StoredProjection | None:
        try:
            async with self._transaction_factory() as session:
                row = (
                    await session.execute(
                        text(LOAD_PROJECTION),
                        {
                            "workspace_id": workspace_id,
                            "projection_name": projection_name,
                        },
                    )
                ).mappings().one_or_none()
                return None if row is None else stored_projection_from_row(row)
        except (SQLAlchemyError, ValidationError) as error:
            raise JournalStorageError("projection storage operation failed") from error

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
        return await self._write(checkpoint, expected, statement)

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
        return await self._write(checkpoint, expected, statement)

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
        try:
            async with self._transaction_factory() as session:
                row = (
                    await session.execute(
                        text(MARK_PROJECTION_UNHEALTHY),
                        {
                            "workspace_id": workspace_id,
                            "projection_name": projection_name,
                            "projection_status": health.value,
                            "failure_code": failure_code,
                            "expected_generation": expected.generation,
                            "expected_sequence": expected.last_journal_sequence,
                        },
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise ProjectionStoreConflict
                return stored_projection_from_row(row)
        except ProjectionStoreConflict:
            raise
        except (SQLAlchemyError, ValidationError) as error:
            raise JournalStorageError("projection storage operation failed") from error

    async def _write(
        self,
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
        }
        if expected.generation is not None:
            parameters["expected_generation"] = expected.generation
            parameters["expected_sequence"] = expected.last_journal_sequence
        try:
            async with self._transaction_factory() as session:
                row = (
                    await session.execute(text(statement), parameters)
                ).mappings().one_or_none()
                if row is None:
                    raise ProjectionStoreConflict
                return stored_projection_from_row(row)
        except ProjectionStoreConflict:
            raise
        except (SQLAlchemyError, ValidationError) as error:
            raise JournalStorageError("projection storage operation failed") from error
