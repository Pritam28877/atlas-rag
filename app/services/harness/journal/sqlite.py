"""Single-owner, off-event-loop SQLite event journal."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.harness.journal.contracts import (
    AppendRequest,
    AppendResult,
    AppendStatus,
    GlobalJournalPage,
    GlobalJournalReadRequest,
    JournalEvent,
    JournalPage,
    JournalReadRequest,
    raise_expected_sequence_conflict,
    raise_idempotency_conflict,
)
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.projection_contracts import ProjectionDefinition
from app.services.harness.journal.projection_online import (
    prepare_online_projections,
)
from app.services.harness.journal.receipts import build_append_result
from app.services.harness.journal.sqlite_connection import (
    SQLiteConnectionOwner,
)
from app.services.harness.journal.sqlite_projection_apply import (
    apply_online_projections,
)
from app.services.harness.protocol import EventRecord


class SQLiteEventJournal:
    """Owns one SQLite connection and one bounded worker thread."""

    def __init__(
        self,
        connection_owner: SQLiteConnectionOwner,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        projections: Sequence[ProjectionDefinition[Any]] = (),
    ) -> None:
        self._connection_owner = connection_owner
        self._clock = clock
        self._projections = prepare_online_projections(projections)

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 64,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        projections: Sequence[ProjectionDefinition[Any]] = (),
    ) -> SQLiteEventJournal:
        connection_owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await connection_owner.initialize()
        return cls(
            connection_owner,
            clock=clock,
            projections=projections,
        )

    async def append(self, request: AppendRequest) -> AppendResult:
        return await self._connection_owner.execute(
            lambda connection: self._append(connection, request)
        )

    async def read_aggregate(self, request: JournalReadRequest) -> JournalPage:
        return await self._connection_owner.execute(
            lambda connection: self._read_aggregate(connection, request)
        )

    async def read_global(
        self,
        request: GlobalJournalReadRequest,
    ) -> GlobalJournalPage:
        return await self._connection_owner.execute(
            lambda connection: self._read_global(connection, request)
        )

    async def close(self) -> None:
        await self._connection_owner.close()

    def _append(
        self,
        connection: sqlite3.Connection,
        request: AppendRequest,
    ) -> AppendResult:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_sequence = self._current_sequence(
                connection,
                request.workspace_id,
                request.aggregate_id,
            )
            replay = self._idempotent_replay(
                connection,
                request,
                current_sequence,
            )
            if replay is not None:
                connection.commit()
                return replay
            if current_sequence != request.expected_sequence:
                raise_expected_sequence_conflict(current_sequence)

            prior_journal_sequence = 0
            if self._projections:
                prior_journal_sequence = self._last_workspace_journal_sequence(
                    connection,
                    request.workspace_id,
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO harness_aggregates
                    (aggregate_id, workspace_id, current_sequence)
                VALUES (?, ?, 0)
                """,
                (request.aggregate_id, request.workspace_id),
            )
            committed_at = self._utc_now()
            journal_sequences: list[int] = []
            for event in request.events:
                event_json = event.model_dump_json()
                inserted = connection.execute(
                    """
                    INSERT INTO harness_events (
                        event_id, workspace_id, aggregate_id, aggregate_sequence,
                        event_json, event_sha256, request_sha256, durability,
                        committed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        request.workspace_id,
                        event.aggregate_id,
                        event.aggregate_sequence,
                        event_json,
                        hashlib.sha256(event_json.encode()).hexdigest(),
                        request.request_sha256,
                        request.durability.value,
                        committed_at.isoformat(),
                    ),
                )
                if inserted.lastrowid is None:
                    raise JournalStorageError(
                        "journal storage omitted global sequence"
                    )
                journal_sequences.append(int(inserted.lastrowid))
            last_sequence = request.events[-1].aggregate_sequence
            updated = connection.execute(
                """
                UPDATE harness_aggregates
                SET current_sequence = ?
                WHERE aggregate_id = ? AND workspace_id = ?
                    AND current_sequence = ?
                """,
                (
                    last_sequence,
                    request.aggregate_id,
                    request.workspace_id,
                    request.expected_sequence,
                ),
            )
            if updated.rowcount != 1:
                latest_sequence = self._current_sequence(
                    connection,
                    request.workspace_id,
                    request.aggregate_id,
                )
                raise_expected_sequence_conflict(latest_sequence)
            result = build_append_result(
                request,
                committed_at,
                tuple(journal_sequences),
            )
            journal_events = tuple(
                JournalEvent(journal_sequence=journal_sequence, event=event)
                for journal_sequence, event in zip(
                    journal_sequences,
                    request.events,
                    strict=True,
                )
            )
            apply_online_projections(
                connection,
                self._projections,
                request.workspace_id,
                journal_events,
                prior_journal_sequence=prior_journal_sequence,
                updated_at=committed_at,
            )
            connection.execute(
                """
                INSERT INTO harness_idempotency (
                    workspace_id, aggregate_id, idempotency_key,
                    request_sha256, result_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request.workspace_id,
                    request.aggregate_id,
                    request.idempotency_key,
                    request.request_sha256,
                    result.model_dump_json(),
                ),
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

    def _read_aggregate(
        self,
        connection: sqlite3.Connection,
        request: JournalReadRequest,
    ) -> JournalPage:
        rows = connection.execute(
            """
            SELECT event_json
            FROM harness_events
            WHERE workspace_id = ? AND aggregate_id = ?
                AND aggregate_sequence > ?
            ORDER BY aggregate_sequence
            LIMIT ?
            """,
            (
                request.workspace_id,
                request.aggregate_id,
                request.after_sequence,
                request.limit + 1,
            ),
        ).fetchall()
        has_more = len(rows) > request.limit
        events = tuple(
            EventRecord.model_validate_json(row[0])
            for row in rows[: request.limit]
        )
        return JournalPage(
            workspace_id=request.workspace_id,
            aggregate_id=request.aggregate_id,
            after_sequence=request.after_sequence,
            events=events,
            has_more=has_more,
        )

    def _read_global(
        self,
        connection: sqlite3.Connection,
        request: GlobalJournalReadRequest,
    ) -> GlobalJournalPage:
        rows = connection.execute(
            """
            SELECT journal_sequence, event_json
            FROM harness_events
            WHERE workspace_id = ? AND journal_sequence > ?
            ORDER BY journal_sequence
            LIMIT ?
            """,
            (
                request.workspace_id,
                request.after_journal_sequence,
                request.limit + 1,
            ),
        ).fetchall()
        has_more = len(rows) > request.limit
        events = tuple(
            JournalEvent(
                journal_sequence=int(row[0]),
                event=EventRecord.model_validate_json(row[1]),
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
    def _last_workspace_journal_sequence(
        connection: sqlite3.Connection,
        workspace_id: str,
    ) -> int:
        value = connection.execute(
            """
            SELECT MAX(journal_sequence)
            FROM harness_events
            WHERE workspace_id = ?
            """,
            (workspace_id,),
        ).fetchone()[0]
        return 0 if value is None else int(value)

    @staticmethod
    def _current_sequence(
        connection: sqlite3.Connection,
        workspace_id: str,
        aggregate_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT current_sequence
            FROM harness_aggregates
            WHERE workspace_id = ? AND aggregate_id = ?
            """,
            (workspace_id, aggregate_id),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def _idempotent_replay(
        self,
        connection: sqlite3.Connection,
        request: AppendRequest,
        current_sequence: int,
    ) -> AppendResult | None:
        row = connection.execute(
            """
            SELECT request_sha256, result_json
            FROM harness_idempotency
            WHERE workspace_id = ? AND aggregate_id = ?
                AND idempotency_key = ?
            """,
            (
                request.workspace_id,
                request.aggregate_id,
                request.idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        if row[0] != request.request_sha256:
            raise_idempotency_conflict(current_sequence)
        stored = AppendResult.model_validate_json(row[1])
        return stored.model_copy(update={"status": AppendStatus.IDEMPOTENT_REPLAY})

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("journal clock must return UTC")
        return value
