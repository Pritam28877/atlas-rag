"""Transactional SQLite mutations for artifact retention evidence."""

import sqlite3
from datetime import datetime

from app.services.harness.journal.errors import RetentionStoreConflict
from app.services.harness.protocol.retention import (
    ArtifactLegalHold,
    ArtifactReference,
    ArtifactTombstone,
    RetainedBlob,
)


def insert_blob(connection: sqlite3.Connection, blob: RetainedBlob) -> None:
    _insert_idempotent(
        connection,
        """
        INSERT OR IGNORE INTO harness_retained_blobs (
            workspace_id, content_sha256, size_bytes, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            blob.workspace_id,
            blob.content_sha256,
            blob.size_bytes,
            blob.created_at.isoformat(),
        ),
    )


def insert_reference(
    connection: sqlite3.Connection,
    reference: ArtifactReference,
) -> None:
    _insert_idempotent(
        connection,
        """
        INSERT OR IGNORE INTO harness_artifact_references (
            workspace_id, reference_sha256, content_sha256, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            reference.workspace_id,
            reference.reference_sha256,
            reference.content_sha256,
            reference.created_at.isoformat(),
        ),
    )


def insert_tombstone(
    connection: sqlite3.Connection,
    tombstone: ArtifactTombstone,
) -> None:
    _insert_idempotent(
        connection,
        """
        INSERT OR IGNORE INTO harness_artifact_tombstones (
            workspace_id, content_sha256, tombstoned_at,
            delete_after, reason
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            tombstone.workspace_id,
            tombstone.content_sha256,
            tombstone.tombstoned_at.isoformat(),
            tombstone.delete_after.isoformat(),
            tombstone.reason,
        ),
    )


def insert_hold(
    connection: sqlite3.Connection,
    hold: ArtifactLegalHold,
) -> None:
    _insert_idempotent(
        connection,
        """
        INSERT OR IGNORE INTO harness_artifact_legal_holds (
            workspace_id, hold_sha256, content_sha256, placed_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            hold.workspace_id,
            hold.hold_sha256,
            hold.content_sha256,
            hold.placed_at.isoformat(),
        ),
    )


def release_reference(
    connection: sqlite3.Connection,
    workspace_id: str,
    reference_sha256: str,
    released_at: datetime,
) -> None:
    _release(
        connection,
        table="harness_artifact_references",
        identity_column="reference_sha256",
        workspace_id=workspace_id,
        identity_sha256=reference_sha256,
        released_at=released_at,
    )


def release_hold(
    connection: sqlite3.Connection,
    workspace_id: str,
    hold_sha256: str,
    released_at: datetime,
) -> None:
    _release(
        connection,
        table="harness_artifact_legal_holds",
        identity_column="hold_sha256",
        workspace_id=workspace_id,
        identity_sha256=hold_sha256,
        released_at=released_at,
    )


def _insert_idempotent(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(statement, parameters)
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise RetentionStoreConflict(
            "retention evidence conflicts with durable state"
        ) from error
    except BaseException:
        connection.rollback()
        raise


def _release(
    connection: sqlite3.Connection,
    *,
    table: str,
    identity_column: str,
    workspace_id: str,
    identity_sha256: str,
    released_at: datetime,
) -> None:
    allowed_targets = {
        ("harness_artifact_references", "reference_sha256"),
        ("harness_artifact_legal_holds", "hold_sha256"),
    }
    if (table, identity_column) not in allowed_targets:
        raise ValueError("unsupported retention release target")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            f"""
            SELECT released_at FROM {table}
            WHERE workspace_id = ? AND {identity_column} = ?
            """,
            (workspace_id, identity_sha256),
        ).fetchone()
        if row is None:
            raise RetentionStoreConflict("retention evidence does not exist")
        if row["released_at"] is not None:
            if row["released_at"] == released_at.isoformat():
                connection.commit()
                return
            raise RetentionStoreConflict(
                "retention evidence was already released"
            )
        connection.execute(
            f"""
            UPDATE {table} SET released_at = ?
            WHERE workspace_id = ? AND {identity_column} = ?
            """,
            (released_at.isoformat(), workspace_id, identity_sha256),
        )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise RetentionStoreConflict("retention release is invalid") from error
    except BaseException:
        connection.rollback()
        raise
