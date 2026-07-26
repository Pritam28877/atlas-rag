"""Transactional SQLite blob admission and reservation lifecycle."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.services.harness.journal.errors import RetentionStoreConflict
from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.journal.sqlite_storage_rows import (
    allowed_reservation,
    load_reservation,
    required_reservation,
    seal_reason,
    storage_state,
)
from app.services.harness.protocol import Sha256, WorkspaceId
from app.services.harness.protocol.retention import (
    BlobReservation,
    BlobReservationStatus,
    RetainedBlob,
    RetentionPolicy,
    StorageReservationDecision,
    StorageSealReason,
    WorkspaceStorageState,
    WriteAdmission,
)


class SQLiteStorageStore:
    def __init__(self, connection_owner: SQLiteConnectionOwner) -> None:
        self._connection_owner = connection_owner

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 32,
    ) -> SQLiteStorageStore:
        owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await owner.initialize()
        return cls(owner)

    async def close(self) -> None:
        await self._connection_owner.close()

    async def reserve(
        self,
        policy: RetentionPolicy,
        workspace_id: WorkspaceId,
        content_sha256: Sha256,
        size_bytes: int,
        *,
        available_filesystem_bytes: int,
        reserved_at: datetime,
    ) -> StorageReservationDecision:
        self._require_utc(reserved_at)
        if not 1 <= size_bytes <= 4 * 1024 * 1024 * 1024:
            raise ValueError("reservation size must be between 1 and 4294967296")
        if not 0 <= available_filesystem_bytes <= 2**63 - 1:
            raise ValueError("available filesystem bytes are invalid")
        return await self._connection_owner.execute(
            lambda connection: self._reserve(
                connection,
                policy,
                workspace_id,
                content_sha256,
                size_bytes,
                available_filesystem_bytes,
                reserved_at,
            )
        )

    async def commit(
        self,
        reservation: BlobReservation,
        *,
        committed_at: datetime,
    ) -> RetainedBlob:
        self._require_utc(committed_at)
        return await self._connection_owner.execute(
            lambda connection: self._commit(
                connection,
                reservation,
                committed_at,
            )
        )

    async def release(
        self,
        reservation: BlobReservation,
        *,
        released_at: datetime,
    ) -> BlobReservation:
        self._require_utc(released_at)
        return await self._connection_owner.execute(
            lambda connection: self._release(
                connection,
                reservation,
                released_at,
            )
        )

    async def state(
        self,
        workspace_id: WorkspaceId,
        *,
        available_filesystem_bytes: int,
    ) -> WorkspaceStorageState:
        return await self._connection_owner.execute(
            lambda connection: self._state(
                connection,
                workspace_id,
                available_filesystem_bytes,
            )
        )

    @classmethod
    def _reserve(
        cls,
        connection: sqlite3.Connection,
        policy: RetentionPolicy,
        workspace_id: str,
        content_sha256: str,
        size_bytes: int,
        available_filesystem_bytes: int,
        reserved_at: datetime,
    ) -> StorageReservationDecision:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO harness_workspace_storage (
                    workspace_id, updated_at
                ) VALUES (?, ?)
                """,
                (workspace_id, reserved_at.isoformat()),
            )
            existing = load_reservation(connection, workspace_id, content_sha256)
            if existing is not None:
                if existing.size_bytes != size_bytes:
                    raise RetentionStoreConflict(
                        "blob reservation size conflicts with durable evidence"
                    )
                if existing.status is BlobReservationStatus.COMMITTED:
                    connection.commit()
                    return allowed_reservation(
                        existing,
                        already_committed=True,
                    )
                if existing.status is BlobReservationStatus.RESERVED:
                    connection.commit()
                    return allowed_reservation(existing)
            elif connection.execute(
                """
                SELECT 1 FROM harness_retained_blobs
                WHERE workspace_id = ? AND content_sha256 = ?
                """,
                (workspace_id, content_sha256),
            ).fetchone() is not None:
                raise RetentionStoreConflict(
                    "retained blob requires storage reconciliation"
                )
            state = storage_state(
                connection,
                workspace_id,
                available_filesystem_bytes,
            )
            reason = seal_reason(policy, state, size_bytes)
            if reason is not None:
                if reason is not StorageSealReason.ALREADY_SEALED:
                    connection.execute(
                        """
                        UPDATE harness_workspace_storage
                        SET sealed = 1, seal_reason = ?, updated_at = ?
                        WHERE workspace_id = ?
                        """,
                        (
                            reason.value,
                            reserved_at.isoformat(),
                            workspace_id,
                        ),
                    )
                connection.commit()
                return StorageReservationDecision(
                    admission=WriteAdmission(
                        allowed=False,
                        seal_required=True,
                        reason=reason,
                    )
                )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO harness_blob_reservations (
                        workspace_id, content_sha256, size_bytes,
                        reservation_status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'reserved', ?, ?)
                    """,
                    (
                        workspace_id,
                        content_sha256,
                        size_bytes,
                        reserved_at.isoformat(),
                        reserved_at.isoformat(),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE harness_blob_reservations
                    SET reservation_status = 'reserved', updated_at = ?
                    WHERE workspace_id = ? AND content_sha256 = ?
                    """,
                    (reserved_at.isoformat(), workspace_id, content_sha256),
                )
            reservation = required_reservation(
                connection,
                workspace_id,
                content_sha256,
            )
            connection.commit()
            return allowed_reservation(reservation)
        except BaseException:
            connection.rollback()
            raise

    @classmethod
    def _commit(
        cls,
        connection: sqlite3.Connection,
        reservation: BlobReservation,
        committed_at: datetime,
    ) -> RetainedBlob:
        connection.execute("BEGIN IMMEDIATE")
        try:
            stored = required_reservation(
                connection,
                reservation.workspace_id,
                reservation.content_sha256,
            )
            if stored.size_bytes != reservation.size_bytes:
                raise RetentionStoreConflict("blob reservation size changed")
            connection.execute(
                """
                INSERT OR IGNORE INTO harness_retained_blobs (
                    workspace_id, content_sha256, size_bytes, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    stored.workspace_id,
                    stored.content_sha256,
                    stored.size_bytes,
                    committed_at.isoformat(),
                ),
            )
            if stored.status is BlobReservationStatus.RESERVED:
                connection.execute(
                    """
                    UPDATE harness_blob_reservations
                    SET reservation_status = 'committed', updated_at = ?
                    WHERE workspace_id = ? AND content_sha256 = ?
                    """,
                    (
                        committed_at.isoformat(),
                        stored.workspace_id,
                        stored.content_sha256,
                    ),
                )
            elif stored.status is not BlobReservationStatus.COMMITTED:
                raise RetentionStoreConflict("blob reservation is not active")
            row = connection.execute(
                """
                SELECT size_bytes, created_at FROM harness_retained_blobs
                WHERE workspace_id = ? AND content_sha256 = ?
                """,
                (stored.workspace_id, stored.content_sha256),
            ).fetchone()
            if (
                row is None
                or row["size_bytes"] != stored.size_bytes
            ):
                raise RetentionStoreConflict("retained blob evidence conflicts")
            blob = RetainedBlob(
                workspace_id=stored.workspace_id,
                content_sha256=stored.content_sha256,
                size_bytes=row["size_bytes"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            connection.commit()
            return blob
        except BaseException:
            connection.rollback()
            raise

    @classmethod
    def _release(
        cls,
        connection: sqlite3.Connection,
        reservation: BlobReservation,
        released_at: datetime,
    ) -> BlobReservation:
        connection.execute("BEGIN IMMEDIATE")
        try:
            stored = required_reservation(
                connection,
                reservation.workspace_id,
                reservation.content_sha256,
            )
            if stored.status is BlobReservationStatus.COMMITTED:
                raise RetentionStoreConflict("committed reservation cannot release")
            if stored.status is BlobReservationStatus.RESERVED:
                connection.execute(
                    """
                    UPDATE harness_blob_reservations
                    SET reservation_status = 'released', updated_at = ?
                    WHERE workspace_id = ? AND content_sha256 = ?
                    """,
                    (
                        released_at.isoformat(),
                        stored.workspace_id,
                        stored.content_sha256,
                    ),
                )
            released = required_reservation(
                connection,
                stored.workspace_id,
                stored.content_sha256,
            )
            connection.commit()
            return released
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _state(
        connection: sqlite3.Connection,
        workspace_id: str,
        available_filesystem_bytes: int,
    ) -> WorkspaceStorageState:
        return storage_state(
            connection,
            workspace_id,
            available_filesystem_bytes,
        )

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("storage reservation timestamp must use UTC")
