"""Single-owner, off-event-loop SQLite event journal."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from app.services.harness.journal.sqlite_connection import (
    JournalStorageError,
    SQLiteConnectionOwner,
)
from app.services.harness.protocol import EventRecord


class SQLiteEventJournal:
    """Owns one SQLite connection and one bounded worker thread."""

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
    ) -> SQLiteEventJournal:
        connection_owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await connection_owner.initialize()
        return cls(connection_owner, clock=clock)

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
            result = self._append_result(
                request,
                committed_at,
                tuple(journal_sequences),
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

    @staticmethod
    def _append_result(
        request: AppendRequest,
        committed_at: datetime,
        journal_sequences: tuple[int, ...],
    ) -> AppendResult:
        event_ids = tuple(event.event_id for event in request.events)
        receipt_values = {
            "aggregate_id": request.aggregate_id,
            "workspace_id": request.workspace_id,
            "durability": request.durability.value,
            "event_ids": event_ids,
            "first_sequence": request.events[0].aggregate_sequence,
            "last_sequence": request.events[-1].aggregate_sequence,
            "request_sha256": request.request_sha256,
            "committed_at": committed_at.isoformat(),
            "journal_sequences": journal_sequences,
        }
        receipt_bytes = json.dumps(
            receipt_values,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return AppendResult(
            workspace_id=request.workspace_id,
            aggregate_id=request.aggregate_id,
            status=AppendStatus.APPENDED,
            durability=request.durability,
            first_sequence=request.events[0].aggregate_sequence,
            last_sequence=request.events[-1].aggregate_sequence,
            event_ids=event_ids,
            journal_sequences=journal_sequences,
            request_sha256=request.request_sha256,
            receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            committed_at=committed_at,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("journal clock must return UTC")
        return value
