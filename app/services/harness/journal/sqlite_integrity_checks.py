"""Streaming logical checks used by the SQLite journal verifier."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import ValidationError

from app.services.harness.journal.contracts import AppendResult, AppendStatus
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.health import JournalCorruptionCode
from app.services.harness.journal.receipts import (
    append_result_receipt_is_valid,
)
from app.services.harness.journal.sqlite_projection_rows import (
    stored_projection_from_row,
)
from app.services.harness.protocol import EventRecord

FETCH_SIZE = 256


@dataclass(slots=True)
class IntegrityCounts:
    events: int = 0
    receipts: int = 0
    projections: int = 0

    @property
    def total(self) -> int:
        return self.events + self.receipts + self.projections


class DetectedSQLiteCorruption(Exception):
    def __init__(
        self,
        code: JournalCorruptionCode,
        *,
        workspace_id: str | None = None,
        projection_name: str | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.workspace_id = workspace_id
        self.projection_name = projection_name


class VerificationLimitReached(Exception):
    pass


class SQLiteIntegrityScanner:
    def __init__(self, maximum_records: int) -> None:
        self._maximum_records = maximum_records
        self.counts = IntegrityCounts()

    def verify(self, connection: sqlite3.Connection) -> IntegrityCounts:
        self._quick_check(connection)
        self._verify_events(connection)
        self._verify_receipts(connection)
        self._verify_projections(connection)
        self._verify_sequences(connection)
        return self.counts

    @staticmethod
    def _quick_check(connection: sqlite3.Connection) -> None:
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
        if row is None or row[0] != "ok":
            raise DetectedSQLiteCorruption(JournalCorruptionCode.SQLITE_QUICK_CHECK)

    def _verify_events(self, connection: sqlite3.Connection) -> None:
        cursor = connection.execute(
            """
            SELECT event_id, aggregate_id, aggregate_sequence,
                   event_json, event_sha256
            FROM harness_events
            ORDER BY journal_sequence
            """
        )
        for row in self._rows(cursor):
            self._claim_record("events")
            try:
                event_json = self._text(row, "event_json")
                event = EventRecord.model_validate_json(event_json)
                columns_match = (
                    event.event_id == self._text(row, "event_id")
                    and event.aggregate_id == self._text(row, "aggregate_id")
                    and event.aggregate_sequence
                    == self._integer(row, "aggregate_sequence")
                )
                digest_matches = hmac.compare_digest(
                    hashlib.sha256(event_json.encode()).hexdigest(),
                    self._text(row, "event_sha256"),
                )
                if not columns_match or not digest_matches:
                    raise ValueError("event storage mismatch")
            except (ValidationError, ValueError, JournalStorageError) as error:
                raise DetectedSQLiteCorruption(
                    JournalCorruptionCode.EVENT_CORRUPT
                ) from error

    def _verify_receipts(self, connection: sqlite3.Connection) -> None:
        cursor = connection.execute(
            """
            SELECT workspace_id, aggregate_id, request_sha256, result_json
            FROM harness_idempotency
            ORDER BY workspace_id, aggregate_id, idempotency_key
            """
        )
        for row in self._rows(cursor):
            self._claim_record("receipts")
            try:
                result = AppendResult.model_validate_json(
                    self._text(row, "result_json")
                )
                scope_matches = (
                    result.status is AppendStatus.APPENDED
                    and result.workspace_id == self._text(row, "workspace_id")
                    and result.aggregate_id == self._text(row, "aggregate_id")
                    and result.request_sha256 == self._text(row, "request_sha256")
                )
                events_match = self._receipt_events_match(connection, result)
                if (
                    not scope_matches
                    or not events_match
                    or not append_result_receipt_is_valid(result)
                ):
                    raise ValueError("receipt storage mismatch")
            except (ValidationError, ValueError, JournalStorageError) as error:
                raise DetectedSQLiteCorruption(
                    JournalCorruptionCode.RECEIPT_CORRUPT
                ) from error

    @staticmethod
    def _receipt_events_match(
        connection: sqlite3.Connection,
        result: AppendResult,
    ) -> bool:
        placeholders = ",".join("?" for _ in result.journal_sequences)
        rows = connection.execute(
            f"""
            SELECT journal_sequence, event_id, workspace_id, aggregate_id,
                   aggregate_sequence, request_sha256, durability
            FROM harness_events
            WHERE workspace_id = ?
              AND journal_sequence IN ({placeholders})
            ORDER BY journal_sequence
            """,
            (result.workspace_id, *result.journal_sequences),
        ).fetchall()
        if len(rows) != len(result.journal_sequences):
            return False
        for event_offset, row in enumerate(rows):
            expected_aggregate_sequence = result.first_sequence + event_offset
            if (
                row["journal_sequence"] != result.journal_sequences[event_offset]
                or row["event_id"] != result.event_ids[event_offset]
                or row["workspace_id"] != result.workspace_id
                or row["aggregate_id"] != result.aggregate_id
                or row["aggregate_sequence"] != expected_aggregate_sequence
                or row["request_sha256"] != result.request_sha256
                or row["durability"] != result.durability.value
            ):
                return False
        return True

    def _verify_projections(self, connection: sqlite3.Connection) -> None:
        cursor = connection.execute(
            "SELECT * FROM harness_projection_checkpoints "
            "ORDER BY workspace_id, projection_name"
        )
        for row in self._rows(cursor):
            self._claim_record("projections")
            try:
                stored_projection_from_row(row)
            except (ValidationError, ValueError, JournalStorageError) as error:
                raise DetectedSQLiteCorruption(
                    JournalCorruptionCode.PROJECTION_CORRUPT,
                    workspace_id=self._optional_text(row, "workspace_id"),
                    projection_name=self._optional_text(row, "projection_name"),
                ) from error

    @staticmethod
    def _verify_sequences(connection: sqlite3.Connection) -> None:
        mismatch = connection.execute(
            """
            WITH event_totals AS (
                SELECT workspace_id, aggregate_id,
                       COUNT(*) AS event_count,
                       MAX(aggregate_sequence) AS maximum_sequence
                FROM harness_events
                GROUP BY workspace_id, aggregate_id
            )
            SELECT 1
            FROM harness_aggregates AS aggregate
            LEFT JOIN event_totals AS totals
              ON totals.workspace_id = aggregate.workspace_id
             AND totals.aggregate_id = aggregate.aggregate_id
            WHERE aggregate.current_sequence
                    <> COALESCE(totals.maximum_sequence, 0)
               OR aggregate.current_sequence
                    <> COALESCE(totals.event_count, 0)
            UNION ALL
            SELECT 1
            FROM event_totals AS totals
            LEFT JOIN harness_aggregates AS aggregate
              ON aggregate.workspace_id = totals.workspace_id
             AND aggregate.aggregate_id = totals.aggregate_id
            WHERE aggregate.workspace_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if mismatch is not None:
            raise DetectedSQLiteCorruption(JournalCorruptionCode.SEQUENCE_CORRUPT)
        position_mismatch = connection.execute(
            """
            WITH event_positions AS (
                SELECT workspace_id, MAX(journal_sequence) AS maximum_sequence
                FROM harness_events
                GROUP BY workspace_id
            )
            SELECT 1
            FROM harness_journal_positions AS position
            LEFT JOIN event_positions AS events
              ON events.workspace_id = position.workspace_id
            WHERE position.current_sequence
                    <> COALESCE(events.maximum_sequence, 0)
            UNION ALL
            SELECT 1
            FROM event_positions AS events
            LEFT JOIN harness_journal_positions AS position
              ON position.workspace_id = events.workspace_id
            WHERE position.workspace_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if position_mismatch is not None:
            raise DetectedSQLiteCorruption(
                JournalCorruptionCode.SEQUENCE_CORRUPT
            )

    @staticmethod
    def _rows(cursor: sqlite3.Cursor) -> Iterator[sqlite3.Row]:
        while rows := cursor.fetchmany(FETCH_SIZE):
            yield from rows

    def _claim_record(self, category: str) -> None:
        if self.counts.total >= self._maximum_records:
            raise VerificationLimitReached
        if category == "events":
            self.counts.events += 1
        elif category == "receipts":
            self.counts.receipts += 1
        else:
            self.counts.projections += 1

    @staticmethod
    def _text(row: sqlite3.Row, field: str) -> str:
        value = row[field]
        if not isinstance(value, str):
            raise JournalStorageError("journal text field is invalid")
        return value

    @staticmethod
    def _optional_text(row: sqlite3.Row, field: str) -> str | None:
        value = row[field]
        return value if isinstance(value, str) else None

    @staticmethod
    def _integer(row: sqlite3.Row, field: str) -> int:
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise JournalStorageError("journal integer field is invalid")
        return value
