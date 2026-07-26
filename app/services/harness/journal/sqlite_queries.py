"""Bounded SQLite journal reads and cursor lookups."""

import sqlite3

from app.services.harness.journal.contracts import (
    GlobalJournalPage,
    GlobalJournalReadRequest,
    JournalEvent,
    JournalPage,
    JournalReadRequest,
)
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.protocol import EventRecord


def read_aggregate(
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


def read_global(
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


def journal_position(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> int:
    connection.execute(
        """
        INSERT OR IGNORE INTO harness_journal_positions (
            workspace_id, current_sequence
        ) VALUES (?, 0)
        """,
        (workspace_id,),
    )
    row = connection.execute(
        """
        SELECT current_sequence
        FROM harness_journal_positions
        WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise JournalStorageError("journal position is missing")
    return int(row[0])


def current_sequence(
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
