"""Strict conversion of SQLite projection rows to domain records."""

import sqlite3
from datetime import datetime

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.projection_contracts import ProjectionCheckpoint
from app.services.harness.journal.projection_store import (
    ProjectionHealth,
    StoredProjection,
)


def stored_projection_from_row(row: sqlite3.Row) -> StoredProjection:
    checkpoint = ProjectionCheckpoint(
        workspace_id=_text(row, "workspace_id"),
        projection_name=_text(row, "projection_name"),
        projection_version=_text(row, "projection_version"),
        last_journal_sequence=_integer(row, "last_journal_sequence"),
        event_count=_integer(row, "event_count"),
        state_json=_text(row, "state_json"),
        state_sha256=_text(row, "state_sha256"),
    )
    try:
        updated_at = datetime.fromisoformat(_text(row, "updated_at"))
        health = ProjectionHealth(_text(row, "projection_status"))
    except ValueError as error:
        raise JournalStorageError("projection record is invalid") from error
    failure_value = row["failure_code"]
    if failure_value is not None and not isinstance(failure_value, str):
        raise JournalStorageError("projection failure code is invalid")
    return StoredProjection(
        generation=_integer(row, "generation"),
        checkpoint=checkpoint,
        health=health,
        failure_code=failure_value,
        updated_at=updated_at,
    )


def _integer(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalStorageError("projection integer field is invalid")
    return value


def _text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str):
        raise JournalStorageError("projection text field is invalid")
    return value
