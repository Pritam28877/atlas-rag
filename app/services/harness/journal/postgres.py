"""Async PostgreSQL implementation of the Atlas event journal."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import cast

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.harness.journal.contracts import (
    MAXIMUM_SEQUENCE,
    AppendRequest,
    AppendResult,
    AppendStatus,
    GlobalJournalPage,
    GlobalJournalReadRequest,
    JournalConflictError,
    JournalDurability,
    JournalEvent,
    JournalPage,
    JournalReadRequest,
    raise_expected_sequence_conflict,
    raise_idempotency_conflict,
)
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.postgres_batch import insert_event_batch
from app.services.harness.journal.postgres_sql import (
    BUFFERED_COMMIT,
    DATABASE_TIMESTAMP,
    INSERT_AGGREGATE,
    INSERT_IDEMPOTENCY,
    INSERT_POSITION,
    LOCK_AGGREGATE,
    LOCK_POSITION,
    READ_AGGREGATE,
    READ_GLOBAL,
    READ_IDEMPOTENCY,
    SYNCHRONOUS_COMMIT,
    UPDATE_AGGREGATE,
    UPDATE_POSITION,
)
from app.services.harness.journal.receipts import build_append_result
from app.services.harness.protocol import EventRecord

type TransactionFactory = Callable[
    [],
    AbstractAsyncContextManager[AsyncSession],
]


class PostgresEventJournal:
    """Uses caller-owned bounded sessions and transaction lifecycle."""

    def __init__(self, transaction_factory: TransactionFactory) -> None:
        self._transaction_factory = transaction_factory

    async def append(self, request: AppendRequest) -> AppendResult:
        try:
            async with self._transaction_factory() as session:
                return await self._append(session, request)
        except JournalConflictError:
            raise
        except (SQLAlchemyError, ValidationError) as error:
            raise JournalStorageError("journal storage operation failed") from error

    async def read_aggregate(self, request: JournalReadRequest) -> JournalPage:
        try:
            async with self._transaction_factory() as session:
                return await self._read_aggregate(session, request)
        except (SQLAlchemyError, ValidationError) as error:
            raise JournalStorageError("journal storage operation failed") from error

    async def read_global(
        self,
        request: GlobalJournalReadRequest,
    ) -> GlobalJournalPage:
        try:
            async with self._transaction_factory() as session:
                return await self._read_global(session, request)
        except (SQLAlchemyError, ValidationError) as error:
            raise JournalStorageError("journal storage operation failed") from error

    async def _append(
        self,
        session: AsyncSession,
        request: AppendRequest,
    ) -> AppendResult:
        await self._set_durability(session, request.durability)
        scope = {
            "workspace_id": request.workspace_id,
            "aggregate_id": request.aggregate_id,
        }
        await session.execute(text(INSERT_AGGREGATE), scope)
        current_sequence_value = await session.scalar(text(LOCK_AGGREGATE), scope)
        if current_sequence_value is None:
            raise JournalStorageError("journal aggregate lock failed")
        current_sequence = int(current_sequence_value)
        replay = await self._idempotent_replay(
            session,
            request,
            current_sequence,
        )
        if replay is not None:
            return replay
        if current_sequence != request.expected_sequence:
            raise_expected_sequence_conflict(current_sequence)

        await session.execute(
            text(INSERT_POSITION),
            {"workspace_id": request.workspace_id},
        )
        journal_position_value = await session.scalar(
            text(LOCK_POSITION),
            {"workspace_id": request.workspace_id},
        )
        if journal_position_value is None:
            raise JournalStorageError("journal position lock failed")
        journal_position = int(journal_position_value)
        last_journal_sequence = journal_position + len(request.events)
        if last_journal_sequence > MAXIMUM_SEQUENCE:
            raise JournalStorageError("journal position capacity is exhausted")
        journal_sequences = tuple(
            range(journal_position + 1, last_journal_sequence + 1)
        )
        committed_at_value = await session.scalar(text(DATABASE_TIMESTAMP))
        if not isinstance(committed_at_value, datetime):
            raise JournalStorageError("journal database timestamp is invalid")
        await insert_event_batch(
            session,
            request,
            committed_at_value,
            journal_sequences,
        )
        result = build_append_result(
            request,
            committed_at_value,
            journal_sequences,
        )
        updated_sequence = await session.scalar(
            text(UPDATE_AGGREGATE),
            {
                **scope,
                "expected_sequence": request.expected_sequence,
                "last_sequence": result.last_sequence,
            },
        )
        if updated_sequence != result.last_sequence:
            raise_expected_sequence_conflict(current_sequence)
        updated_position = await session.scalar(
            text(UPDATE_POSITION),
            {
                "workspace_id": request.workspace_id,
                "expected_journal_sequence": journal_position,
                "last_journal_sequence": last_journal_sequence,
            },
        )
        if updated_position != last_journal_sequence:
            raise JournalStorageError("journal position update failed")
        await session.execute(
            text(INSERT_IDEMPOTENCY),
            {
                **scope,
                "idempotency_key": request.idempotency_key,
                "request_sha256": request.request_sha256,
                "result_json": result.model_dump_json(),
            },
        )
        return result

    async def _idempotent_replay(
        self,
        session: AsyncSession,
        request: AppendRequest,
        current_sequence: int,
    ) -> AppendResult | None:
        replay_row = (
            await session.execute(
                text(READ_IDEMPOTENCY),
                {
                    "workspace_id": request.workspace_id,
                    "aggregate_id": request.aggregate_id,
                    "idempotency_key": request.idempotency_key,
                },
            )
        ).mappings().one_or_none()
        if replay_row is None:
            return None
        stored_request_sha256 = cast(str, replay_row["request_sha256"])
        if stored_request_sha256 != request.request_sha256:
            raise_idempotency_conflict(current_sequence)
        stored_result = AppendResult.model_validate_json(
            cast(str, replay_row["result_json"])
        )
        return stored_result.model_copy(
            update={"status": AppendStatus.IDEMPOTENT_REPLAY}
        )

    async def _read_aggregate(
        self,
        session: AsyncSession,
        request: JournalReadRequest,
    ) -> JournalPage:
        rows = (
            await session.execute(
                text(READ_AGGREGATE),
                {
                    "workspace_id": request.workspace_id,
                    "aggregate_id": request.aggregate_id,
                    "after_sequence": request.after_sequence,
                    "query_limit": request.limit + 1,
                },
            )
        ).mappings().all()
        has_more = len(rows) > request.limit
        events = tuple(
            EventRecord.model_validate_json(cast(str, row["event_json"]))
            for row in rows[: request.limit]
        )
        return JournalPage(
            workspace_id=request.workspace_id,
            aggregate_id=request.aggregate_id,
            after_sequence=request.after_sequence,
            events=events,
            has_more=has_more,
        )

    async def _read_global(
        self,
        session: AsyncSession,
        request: GlobalJournalReadRequest,
    ) -> GlobalJournalPage:
        rows = (
            await session.execute(
                text(READ_GLOBAL),
                {
                    "workspace_id": request.workspace_id,
                    "after_journal_sequence": request.after_journal_sequence,
                    "query_limit": request.limit + 1,
                },
            )
        ).mappings().all()
        has_more = len(rows) > request.limit
        events = tuple(
            JournalEvent(
                journal_sequence=int(row["journal_sequence"]),
                event=EventRecord.model_validate_json(
                    cast(str, row["event_json"])
                ),
            )
            for row in rows[: request.limit]
        )
        return GlobalJournalPage(
            workspace_id=request.workspace_id,
            after_journal_sequence=request.after_journal_sequence,
            events=events,
            has_more=has_more,
        )

    @staticmethod
    async def _set_durability(
        session: AsyncSession,
        durability: JournalDurability,
    ) -> None:
        statement = (
            SYNCHRONOUS_COMMIT
            if durability is JournalDurability.SYNCHRONOUS
            else BUFFERED_COMMIT
        )
        await session.execute(text(statement))
