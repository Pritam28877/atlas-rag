"""Strict conversion of PostgreSQL projection rows to domain records."""

from datetime import datetime
from typing import cast

from sqlalchemy.engine import RowMapping

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.projection_contracts import ProjectionCheckpoint
from app.services.harness.journal.projection_store import (
    ProjectionHealth,
    StoredProjection,
)


def stored_projection_from_row(row: RowMapping) -> StoredProjection:
    checkpoint = ProjectionCheckpoint(
        workspace_id=cast(str, row["workspace_id"]),
        projection_name=cast(str, row["projection_name"]),
        projection_version=cast(str, row["projection_version"]),
        last_journal_sequence=_bigint(row, "last_journal_sequence"),
        event_count=_bigint(row, "event_count"),
        state_json=cast(str, row["state_json"]),
        state_sha256=cast(str, row["state_sha256"]),
    )
    updated_at = row["updated_at"]
    if not isinstance(updated_at, datetime):
        raise JournalStorageError("projection timestamp is invalid")
    try:
        health = ProjectionHealth(cast(str, row["projection_status"]))
    except ValueError as error:
        raise JournalStorageError("projection health is invalid") from error
    return StoredProjection(
        generation=_bigint(row, "generation"),
        checkpoint=checkpoint,
        health=health,
        failure_code=cast(str | None, row["failure_code"]),
        updated_at=updated_at,
    )


def _bigint(row: RowMapping, field: str) -> int:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalStorageError("projection integer field is invalid")
    return value
