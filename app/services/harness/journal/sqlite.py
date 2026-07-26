"""Single-owner, off-event-loop SQLite event journal."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.harness.journal.contracts import (
    MAXIMUM_SEQUENCE,
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
from app.services.harness.journal.faults import JournalFaultPoint
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
from app.services.harness.journal.sqlite_queries import (
    current_sequence,
    journal_position,
    read_aggregate,
    read_global,
)


class SQLiteEventJournal:
    """Owns one SQLite connection and one bounded worker thread."""

    def __init__(
        self,
        connection_owner: SQLiteConnectionOwner,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        projections: Sequence[ProjectionDefinition[Any]] = (),
        fault_injector: Callable[[JournalFaultPoint], None] | None = None,
    ) -> None:
        self._connection_owner = connection_owner
        self._clock = clock
        self._projections = prepare_online_projections(projections)
        self._fault_injector = fault_injector

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 64,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        projections: Sequence[ProjectionDefinition[Any]] = (),
        fault_injector: Callable[[JournalFaultPoint], None] | None = None,
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
            fault_injector=fault_injector,
        )

    async def append(self, request: AppendRequest) -> AppendResult:
        return await self._connection_owner.execute(
            lambda connection: self._append(connection, request)
        )

    async def read_aggregate(self, request: JournalReadRequest) -> JournalPage:
        return await self._connection_owner.execute(
            lambda connection: read_aggregate(connection, request)
        )

    async def read_global(
        self,
        request: GlobalJournalReadRequest,
    ) -> GlobalJournalPage:
        return await self._connection_owner.execute(
            lambda connection: read_global(connection, request)
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
            aggregate_sequence = current_sequence(
                connection,
                request.workspace_id,
                request.aggregate_id,
            )
            replay = self._idempotent_replay(
                connection,
                request,
                aggregate_sequence,
            )
            if replay is not None:
                connection.commit()
                return replay
            if aggregate_sequence != request.expected_sequence:
                raise_expected_sequence_conflict(aggregate_sequence)

            connection.execute(
                """
                INSERT OR IGNORE INTO harness_aggregates
                    (aggregate_id, workspace_id, current_sequence)
                VALUES (?, ?, 0)
                """,
                (request.aggregate_id, request.workspace_id),
            )
            workspace_position = journal_position(
                connection,
                request.workspace_id,
            )
            last_journal_sequence = workspace_position + len(request.events)
            if last_journal_sequence > MAXIMUM_SEQUENCE:
                raise JournalStorageError(
                    "journal position capacity is exhausted"
                )
            journal_sequences = tuple(
                range(workspace_position + 1, last_journal_sequence + 1)
            )
            committed_at = self._utc_now()
            for event, journal_sequence in zip(
                request.events,
                journal_sequences,
                strict=True,
            ):
                event_json = event.model_dump_json()
                connection.execute(
                    """
                    INSERT INTO harness_events (
                        journal_sequence, event_id, workspace_id, aggregate_id,
                        aggregate_sequence, event_json, event_sha256,
                        request_sha256, durability, committed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        journal_sequence,
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
            self._inject_fault(JournalFaultPoint.AFTER_EVENTS_INSERTED)
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
                latest_sequence = current_sequence(
                    connection,
                    request.workspace_id,
                    request.aggregate_id,
                )
                raise_expected_sequence_conflict(latest_sequence)
            position_updated = connection.execute(
                """
                UPDATE harness_journal_positions
                SET current_sequence = ?
                WHERE workspace_id = ? AND current_sequence = ?
                """,
                (
                    last_journal_sequence,
                    request.workspace_id,
                    workspace_position,
                ),
            )
            if position_updated.rowcount != 1:
                raise JournalStorageError("journal position update failed")
            self._inject_fault(JournalFaultPoint.AFTER_AGGREGATE_UPDATED)
            result = build_append_result(
                request,
                committed_at,
                journal_sequences,
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
                prior_journal_sequence=workspace_position,
                updated_at=committed_at,
            )
            self._inject_fault(JournalFaultPoint.AFTER_PROJECTIONS_APPLIED)
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
            self._inject_fault(JournalFaultPoint.BEFORE_COMMIT)
            connection.commit()
            self._inject_fault(JournalFaultPoint.AFTER_COMMIT)
            return result
        except BaseException:
            connection.rollback()
            raise

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

    def _inject_fault(self, fault_point: JournalFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(fault_point)
