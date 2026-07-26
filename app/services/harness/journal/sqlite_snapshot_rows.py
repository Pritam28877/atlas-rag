"""Strict decoding for SQLite replay snapshot rows."""

import json
import sqlite3
from datetime import datetime

from pydantic import ValidationError

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.protocol.retention import (
    SealedJournalSegment,
    SyncedSnapshot,
    SyncedSnapshotBundle,
)


def load_snapshot_bundle(
    connection: sqlite3.Connection,
    workspace_id: str,
    snapshot_sha256: str,
) -> SyncedSnapshotBundle | None:
    snapshot_row = connection.execute(
        """
        SELECT * FROM harness_synced_snapshots
        WHERE workspace_id = ? AND snapshot_sha256 = ?
        """,
        (workspace_id, snapshot_sha256),
    ).fetchone()
    if snapshot_row is None:
        return None
    segment_rows = connection.execute(
        """
        SELECT * FROM harness_sealed_segments
        WHERE workspace_id = ? AND snapshot_sha256 = ?
        ORDER BY first_journal_sequence
        LIMIT 1001
        """,
        (workspace_id, snapshot_sha256),
    ).fetchall()
    try:
        reachable = json.loads(_text(snapshot_row, "reachable_json"))
        if not isinstance(reachable, list):
            raise ValueError("snapshot reachability must be a list")
        snapshot = SyncedSnapshot(
            workspace_id=_text(snapshot_row, "workspace_id"),
            snapshot_sha256=_text(snapshot_row, "snapshot_sha256"),
            manifest_sha256=_text(snapshot_row, "manifest_sha256"),
            through_journal_sequence=_integer(
                snapshot_row,
                "through_journal_sequence",
            ),
            reachable_content_sha256s=tuple(reachable),
            synced_at=datetime.fromisoformat(_text(snapshot_row, "synced_at")),
        )
        segments = tuple(_segment_from_row(row) for row in segment_rows)
        return SyncedSnapshotBundle(snapshot=snapshot, segments=segments)
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("stored snapshot facts are invalid") from error


def _segment_from_row(row: sqlite3.Row) -> SealedJournalSegment:
    return SealedJournalSegment(
        workspace_id=_text(row, "workspace_id"),
        segment_sha256=_text(row, "segment_sha256"),
        snapshot_sha256=_text(row, "snapshot_sha256"),
        first_journal_sequence=_integer(row, "first_journal_sequence"),
        last_journal_sequence=_integer(row, "last_journal_sequence"),
        event_count=_integer(row, "event_count"),
        sealed_at=datetime.fromisoformat(_text(row, "sealed_at")),
    )


def _integer(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalStorageError("stored snapshot integer is invalid")
    return value


def _text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str):
        raise JournalStorageError("stored snapshot text is invalid")
    return value
