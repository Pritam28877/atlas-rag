"""Strict decoding and pure decisions for SQLite blob reservations."""

import sqlite3
from datetime import datetime

from pydantic import ValidationError

from app.services.harness.journal.errors import (
    JournalStorageError,
    RetentionStoreConflict,
)
from app.services.harness.protocol.retention import (
    BlobReservation,
    BlobReservationStatus,
    RetentionPolicy,
    StorageReservationDecision,
    StorageSealReason,
    WorkspaceStorageState,
    WriteAdmission,
)


def storage_state(
    connection: sqlite3.Connection,
    workspace_id: str,
    available_filesystem_bytes: int,
) -> WorkspaceStorageState:
    row = connection.execute(
        """
        SELECT used_blob_bytes, reserved_blob_bytes, sealed
        FROM harness_workspace_storage WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None:
        return WorkspaceStorageState(
            workspace_id=workspace_id,
            used_blob_bytes=0,
            reserved_blob_bytes=0,
            available_filesystem_bytes=available_filesystem_bytes,
        )
    try:
        return WorkspaceStorageState(
            workspace_id=workspace_id,
            used_blob_bytes=row["used_blob_bytes"],
            reserved_blob_bytes=row["reserved_blob_bytes"],
            available_filesystem_bytes=available_filesystem_bytes,
            sealed=bool(row["sealed"]),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("workspace storage state is invalid") from error


def required_reservation(
    connection: sqlite3.Connection,
    workspace_id: str,
    content_sha256: str,
) -> BlobReservation:
    reservation = load_reservation(connection, workspace_id, content_sha256)
    if reservation is None:
        raise RetentionStoreConflict("blob reservation does not exist")
    return reservation


def load_reservation(
    connection: sqlite3.Connection,
    workspace_id: str,
    content_sha256: str,
) -> BlobReservation | None:
    row = connection.execute(
        """
        SELECT * FROM harness_blob_reservations
        WHERE workspace_id = ? AND content_sha256 = ?
        """,
        (workspace_id, content_sha256),
    ).fetchone()
    if row is None:
        return None
    try:
        return BlobReservation(
            workspace_id=row["workspace_id"],
            content_sha256=row["content_sha256"],
            size_bytes=row["size_bytes"],
            status=BlobReservationStatus(row["reservation_status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("blob reservation is invalid") from error


def seal_reason(
    policy: RetentionPolicy,
    state: WorkspaceStorageState,
    size_bytes: int,
) -> StorageSealReason | None:
    if state.sealed:
        return StorageSealReason.ALREADY_SEALED
    accounted = state.used_blob_bytes + state.reserved_blob_bytes
    if accounted + size_bytes > policy.blob_quota_bytes:
        return StorageSealReason.WORKSPACE_QUOTA
    remaining = (
        state.available_filesystem_bytes
        - state.reserved_blob_bytes
        - size_bytes
    )
    if remaining < policy.disk_reserve_bytes:
        return StorageSealReason.DISK_RESERVE
    return None


def allowed_reservation(
    reservation: BlobReservation,
    *,
    already_committed: bool = False,
) -> StorageReservationDecision:
    return StorageReservationDecision(
        admission=WriteAdmission(allowed=True, seal_required=False),
        reservation=reservation,
        already_committed=already_committed,
    )
