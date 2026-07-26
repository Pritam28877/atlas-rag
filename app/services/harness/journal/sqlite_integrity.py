"""Bounded logical integrity verification for the local SQLite journal."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.health import (
    MAXIMUM_VERIFICATION_RECORDS,
    JournalCorruptionCode,
    JournalCorruptionError,
    JournalHealthRecord,
    JournalHealthStatus,
    JournalVerificationResult,
)
from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.journal.sqlite_integrity_checks import (
    DetectedSQLiteCorruption,
    IntegrityCounts,
    SQLiteIntegrityScanner,
    VerificationLimitReached,
)


class SQLiteJournalIntegrityVerifier:
    """Own a private connection for consistent, bounded verification."""

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
        maximum_pending_operations: int = 4,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> SQLiteJournalIntegrityVerifier:
        connection_owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await connection_owner.initialize()
        return cls(connection_owner, clock=clock)

    async def close(self) -> None:
        await self._connection_owner.close()

    async def health(self) -> JournalHealthRecord:
        return await self._connection_owner.execute(self._health)

    async def verify(
        self,
        *,
        maximum_records: int = 100_000,
    ) -> JournalVerificationResult:
        if not 1 <= maximum_records <= MAXIMUM_VERIFICATION_RECORDS:
            raise ValueError(
                "maximum verification records must be between 1 and 1000000"
            )
        return await self._connection_owner.execute(
            lambda connection: self._verify(connection, maximum_records)
        )

    def _verify(
        self,
        connection: sqlite3.Connection,
        maximum_records: int,
    ) -> JournalVerificationResult:
        scanner = SQLiteIntegrityScanner(maximum_records)
        connection.execute("BEGIN IMMEDIATE")
        try:
            health = self._health(connection)
            if health.status is JournalHealthStatus.NEEDS_OPERATOR:
                raise JournalCorruptionError(
                    JournalCorruptionCode.NEEDS_OPERATOR
                )
            counts = scanner.verify(connection)
            verified_at = self._utc_now()
            update = connection.execute(
                """
                UPDATE harness_journal_health
                SET journal_status = 'healthy',
                    failure_code = NULL,
                    verified_event_count = ?,
                    verified_at = ?
                WHERE singleton = 1 AND generation = ?
                """,
                (
                    counts.events,
                    verified_at.isoformat(),
                    health.generation,
                ),
            )
            if update.rowcount != 1:
                raise JournalStorageError("journal health state changed")
            connection.commit()
            return self._result(counts, complete=True, verified_at=verified_at)
        except VerificationLimitReached:
            connection.rollback()
            return self._result(
                scanner.counts,
                complete=False,
                verified_at=None,
            )
        except DetectedSQLiteCorruption as corruption:
            connection.rollback()
            try:
                self._mark_corrupt(connection, corruption)
            except BaseException as mark_error:
                raise JournalCorruptionError(corruption.code) from mark_error
            raise JournalCorruptionError(corruption.code) from corruption
        except BaseException:
            connection.rollback()
            raise

    def _mark_corrupt(
        self,
        connection: sqlite3.Connection,
        corruption: DetectedSQLiteCorruption,
    ) -> None:
        verified_at = self._utc_now().isoformat()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if corruption.workspace_id and corruption.projection_name:
                connection.execute(
                    """
                    UPDATE harness_projection_checkpoints
                    SET projection_status = 'needs_operator',
                        failure_code = 'checkpoint_corrupt',
                        updated_at = MAX(updated_at, :verified_at)
                    WHERE workspace_id = :workspace_id
                      AND projection_name = :projection_name
                    """,
                    {
                        "workspace_id": corruption.workspace_id,
                        "projection_name": corruption.projection_name,
                        "verified_at": verified_at,
                    },
                )
            update = connection.execute(
                """
                UPDATE harness_journal_health
                SET journal_status = 'needs_operator',
                    failure_code = :failure_code,
                    verified_at = MAX(verified_at, :verified_at)
                WHERE singleton = 1
                """,
                {
                    "failure_code": corruption.code.value,
                    "verified_at": verified_at,
                },
            )
            if update.rowcount != 1:
                raise JournalStorageError("journal health state is missing")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _health(connection: sqlite3.Connection) -> JournalHealthRecord:
        row = connection.execute(
            """
            SELECT generation, journal_status, failure_code,
                   verified_event_count, verified_at
            FROM harness_journal_health WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise JournalStorageError("journal health state is missing")
        try:
            return JournalHealthRecord(
                generation=row["generation"],
                status=JournalHealthStatus(row["journal_status"]),
                failure_code=row["failure_code"],
                verified_event_count=row["verified_event_count"],
                verified_at=datetime.fromisoformat(row["verified_at"]),
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise JournalStorageError("journal health state is invalid") from error

    @staticmethod
    def _result(
        counts: IntegrityCounts,
        *,
        complete: bool,
        verified_at: datetime | None,
    ) -> JournalVerificationResult:
        return JournalVerificationResult(
            complete=complete,
            events_checked=counts.events,
            receipts_checked=counts.receipts,
            projections_checked=counts.projections,
            verified_at=verified_at,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("journal verification clock must return UTC")
        return value
