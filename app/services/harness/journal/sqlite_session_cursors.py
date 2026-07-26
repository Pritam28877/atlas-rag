"""Transactional monotonic subscription-cursor persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Never

from app.services.harness.journal.errors import (
    JournalStorageError,
    SessionStoreConflict,
    SessionStoreConflictCode,
)
from app.services.harness.journal.sqlite_session_rows import (
    decode_subscription_cursor,
    load_subscription_cursor_row,
)
from app.services.harness.protocol import SubscriptionCursorRecord


def save_subscription_cursor(
    connection: sqlite3.Connection,
    cursor: SubscriptionCursorRecord,
    maximum_cursors: int,
) -> SubscriptionCursorRecord:
    connection.execute("BEGIN IMMEDIATE")
    try:
        _purge_expired(connection, cursor.updated_at, 256)
        existing_row = load_subscription_cursor_row(
            connection,
            cursor.workspace_id,
            cursor.subscription_id,
        )
        if existing_row is None:
            if cursor.generation != 1:
                _reject(SessionStoreConflictCode.CURSOR_CONFLICT)
            _require_capacity(connection, cursor.workspace_id, maximum_cursors)
            _insert_cursor(connection, cursor)
        else:
            existing = decode_subscription_cursor(existing_row)
            _validate_transition(existing, cursor)
            if existing == cursor:
                connection.commit()
                return existing
            _update_cursor(connection, existing, cursor)
        stored_row = load_subscription_cursor_row(
            connection,
            cursor.workspace_id,
            cursor.subscription_id,
        )
        if stored_row is None:
            raise JournalStorageError("subscription cursor was not stored")
        stored = decode_subscription_cursor(stored_row)
        if stored != cursor:
            raise JournalStorageError("stored subscription cursor differs")
        connection.commit()
        return stored
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise SessionStoreConflict(
            SessionStoreConflictCode.CURSOR_CONFLICT
        ) from error
    except BaseException:
        connection.rollback()
        raise


def load_subscription_cursor(
    connection: sqlite3.Connection,
    workspace_id: str,
    principal_id: str,
    subscription_id: str,
    observed_at: datetime,
) -> SubscriptionCursorRecord | None:
    row = load_subscription_cursor_row(
        connection,
        workspace_id,
        subscription_id,
    )
    if row is None:
        return None
    cursor = decode_subscription_cursor(row)
    if cursor.principal_id != principal_id:
        _reject(SessionStoreConflictCode.OWNER_MISMATCH)
    if cursor.expires_at <= observed_at:
        return None
    return cursor


def purge_expired_subscription_cursors(
    connection: sqlite3.Connection,
    expired_at_or_before: datetime,
    maximum_records: int,
) -> int:
    connection.execute("BEGIN IMMEDIATE")
    try:
        deleted_count = _purge_expired(
            connection,
            expired_at_or_before,
            maximum_records,
        )
        connection.commit()
        return deleted_count
    except BaseException:
        connection.rollback()
        raise


def _insert_cursor(
    connection: sqlite3.Connection,
    cursor: SubscriptionCursorRecord,
) -> None:
    connection.execute(
        """
        INSERT INTO harness_subscription_cursors (
            workspace_id, principal_id, subscription_id, generation,
            acknowledged_sequence, delivered_sequence, updated_at,
            expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _cursor_values(cursor),
    )


def _update_cursor(
    connection: sqlite3.Connection,
    existing: SubscriptionCursorRecord,
    cursor: SubscriptionCursorRecord,
) -> None:
    updated = connection.execute(
        """
        UPDATE harness_subscription_cursors
        SET generation = ?, acknowledged_sequence = ?,
            delivered_sequence = ?, updated_at = ?, expires_at = ?
        WHERE workspace_id = ? AND subscription_id = ?
          AND generation = ?
        """,
        (
            cursor.generation,
            cursor.acknowledged_sequence,
            cursor.delivered_sequence,
            cursor.updated_at.isoformat(),
            cursor.expires_at.isoformat(),
            cursor.workspace_id,
            cursor.subscription_id,
            existing.generation,
        ),
    )
    if updated.rowcount != 1:
        _reject(SessionStoreConflictCode.CURSOR_CONFLICT)


def _cursor_values(cursor: SubscriptionCursorRecord) -> tuple[object, ...]:
    return (
        cursor.workspace_id,
        cursor.principal_id,
        cursor.subscription_id,
        cursor.generation,
        cursor.acknowledged_sequence,
        cursor.delivered_sequence,
        cursor.updated_at.isoformat(),
        cursor.expires_at.isoformat(),
    )


def _validate_transition(
    existing: SubscriptionCursorRecord,
    requested: SubscriptionCursorRecord,
) -> None:
    if existing.principal_id != requested.principal_id:
        _reject(SessionStoreConflictCode.OWNER_MISMATCH)
    if existing == requested:
        return
    if (
        requested.generation != existing.generation + 1
        or requested.acknowledged_sequence
        < existing.acknowledged_sequence
        or requested.delivered_sequence < existing.delivered_sequence
        or requested.updated_at < existing.updated_at
        or requested.expires_at < existing.expires_at
    ):
        _reject(SessionStoreConflictCode.CURSOR_CONFLICT)


def _purge_expired(
    connection: sqlite3.Connection,
    expired_at_or_before: datetime,
    maximum_records: int,
) -> int:
    deleted = connection.execute(
        """
        DELETE FROM harness_subscription_cursors
        WHERE rowid IN (
            SELECT rowid FROM harness_subscription_cursors
            WHERE expires_at <= ?
            ORDER BY expires_at, workspace_id, subscription_id
            LIMIT ?
        )
        """,
        (expired_at_or_before.isoformat(), maximum_records),
    )
    return deleted.rowcount


def _require_capacity(
    connection: sqlite3.Connection,
    workspace_id: str,
    maximum_cursors: int,
) -> None:
    row = connection.execute(
        """
        SELECT COUNT(*) AS record_count
        FROM harness_subscription_cursors
        WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None or not isinstance(row["record_count"], int):
        raise JournalStorageError("subscription cursor count is invalid")
    if row["record_count"] >= maximum_cursors:
        _reject(SessionStoreConflictCode.CAPACITY)


def _reject(code: SessionStoreConflictCode) -> Never:
    raise SessionStoreConflict(code)
