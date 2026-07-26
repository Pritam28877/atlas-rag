"""Strict bounded decoding for SQLite artifact retention evidence."""

import sqlite3
from datetime import datetime

from pydantic import ValidationError

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.protocol.retention import (
    MAXIMUM_TRACKED_BLOBS,
    ArtifactLegalHold,
    ArtifactReference,
    ArtifactTombstone,
    RetainedBlob,
    RetentionEvidence,
)

EVIDENCE_FETCH_LIMIT = MAXIMUM_TRACKED_BLOBS + 1


def load_retention_evidence(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> RetentionEvidence:
    blob_rows = connection.execute(
        """
        SELECT blob.*, tombstone.tombstoned_at, tombstone.delete_after,
               tombstone.reason
        FROM harness_retained_blobs AS blob
        LEFT JOIN harness_artifact_tombstones AS tombstone
          ON tombstone.workspace_id = blob.workspace_id
         AND tombstone.content_sha256 = blob.content_sha256
        WHERE blob.workspace_id = ?
        ORDER BY blob.content_sha256
        LIMIT ?
        """,
        (workspace_id, EVIDENCE_FETCH_LIMIT),
    ).fetchall()
    reference_rows = connection.execute(
        """
        SELECT * FROM harness_artifact_references
        WHERE workspace_id = ?
        ORDER BY reference_sha256
        LIMIT ?
        """,
        (workspace_id, EVIDENCE_FETCH_LIMIT),
    ).fetchall()
    hold_rows = connection.execute(
        """
        SELECT * FROM harness_artifact_legal_holds
        WHERE workspace_id = ?
        ORDER BY hold_sha256
        LIMIT ?
        """,
        (workspace_id, EVIDENCE_FETCH_LIMIT),
    ).fetchall()
    try:
        return RetentionEvidence(
            workspace_id=workspace_id,
            blobs=tuple(_blob(row) for row in blob_rows),
            references=tuple(_reference(row) for row in reference_rows),
            legal_holds=tuple(_hold(row) for row in hold_rows),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("stored retention evidence is invalid") from error


def _blob(row: sqlite3.Row) -> RetainedBlob:
    workspace_id = _text(row, "workspace_id")
    content_sha256 = _text(row, "content_sha256")
    tombstone = None
    if row["tombstoned_at"] is not None:
        tombstone = ArtifactTombstone(
            workspace_id=workspace_id,
            content_sha256=content_sha256,
            tombstoned_at=_timestamp(row, "tombstoned_at"),
            delete_after=_timestamp(row, "delete_after"),
            reason=_text(row, "reason"),
        )
    return RetainedBlob(
        workspace_id=workspace_id,
        content_sha256=content_sha256,
        size_bytes=_integer(row, "size_bytes"),
        created_at=_timestamp(row, "created_at"),
        tombstone=tombstone,
        garbage_collected_at=_optional_timestamp(row, "garbage_collected_at"),
    )


def _reference(row: sqlite3.Row) -> ArtifactReference:
    return ArtifactReference(
        workspace_id=_text(row, "workspace_id"),
        reference_sha256=_text(row, "reference_sha256"),
        content_sha256=_text(row, "content_sha256"),
        created_at=_timestamp(row, "created_at"),
        released_at=_optional_timestamp(row, "released_at"),
    )


def _hold(row: sqlite3.Row) -> ArtifactLegalHold:
    return ArtifactLegalHold(
        workspace_id=_text(row, "workspace_id"),
        hold_sha256=_text(row, "hold_sha256"),
        content_sha256=_text(row, "content_sha256"),
        placed_at=_timestamp(row, "placed_at"),
        released_at=_optional_timestamp(row, "released_at"),
    )


def _integer(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalStorageError("retention evidence integer is invalid")
    return value


def _text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str):
        raise JournalStorageError("retention evidence text is invalid")
    return value


def _timestamp(row: sqlite3.Row, field: str) -> datetime:
    return datetime.fromisoformat(_text(row, field))


def _optional_timestamp(
    row: sqlite3.Row,
    field: str,
) -> datetime | None:
    value = row[field]
    if value is None:
        return None
    if not isinstance(value, str):
        raise JournalStorageError("retention evidence timestamp is invalid")
    return datetime.fromisoformat(value)
