"""Transactional SQLite persistence for bounded startup recovery evidence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.services.harness.journal.errors import RecoveryStoreConflict
from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.journal.sqlite_recovery_rows import (
    MAXIMUM_RECOVERY_RECORDS,
    decode_lease,
    decode_operation,
    load_active_leases,
    load_recoverable_operations,
)
from app.services.harness.protocol import OperationRecord, WorkspaceId
from app.services.harness.protocol.recovery import RecoveryLease


class SQLiteRecoveryStore:
    def __init__(self, connection_owner: SQLiteConnectionOwner) -> None:
        self._connection_owner = connection_owner

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 32,
    ) -> SQLiteRecoveryStore:
        owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await owner.initialize()
        return cls(owner)

    async def close(self) -> None:
        await self._connection_owner.close()

    async def save_operation(
        self,
        workspace_id: WorkspaceId,
        operation: OperationRecord,
        *,
        updated_at: datetime,
    ) -> OperationRecord:
        self._require_utc(updated_at)
        if updated_at < operation.prepared_at:
            raise ValueError("operation update cannot precede preparation")
        return await self._connection_owner.execute(
            lambda connection: self._save_operation(
                connection,
                workspace_id,
                operation,
                updated_at,
            )
        )

    async def load_recoverable_operations(
        self,
        workspace_id: WorkspaceId,
        *,
        maximum_records: int = MAXIMUM_RECOVERY_RECORDS,
    ) -> tuple[OperationRecord, ...]:
        return await self._connection_owner.execute(
            lambda connection: load_recoverable_operations(
                connection,
                workspace_id,
                maximum_records,
            )
        )

    async def save_lease(
        self,
        workspace_id: WorkspaceId,
        lease: RecoveryLease,
        *,
        updated_at: datetime,
    ) -> RecoveryLease:
        self._require_utc(updated_at)
        if lease.expired_at is not None and updated_at < lease.expired_at:
            raise ValueError("lease update cannot precede expiry evidence")
        return await self._connection_owner.execute(
            lambda connection: self._save_lease(
                connection,
                workspace_id,
                lease,
                updated_at,
            )
        )

    async def load_active_leases(
        self,
        workspace_id: WorkspaceId,
        *,
        maximum_records: int = MAXIMUM_RECOVERY_RECORDS,
    ) -> tuple[RecoveryLease, ...]:
        return await self._connection_owner.execute(
            lambda connection: load_active_leases(
                connection,
                workspace_id,
                maximum_records,
            )
        )

    @staticmethod
    def _save_operation(
        connection: sqlite3.Connection,
        workspace_id: str,
        operation: OperationRecord,
        updated_at: datetime,
    ) -> OperationRecord:
        operation_json = operation.model_dump_json()
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """
                SELECT operation_id, operation_state, idempotency_class,
                       operation_json
                FROM harness_recovery_operations
                WHERE workspace_id = ? AND operation_id = ?
                """,
                (workspace_id, operation.operation_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO harness_recovery_operations (
                        workspace_id, operation_id, operation_state,
                        idempotency_class, operation_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        operation.operation_id,
                        operation.state.value,
                        operation.idempotency_class.value,
                        operation_json,
                        updated_at.isoformat(),
                    ),
                )
            else:
                stored = decode_operation(row)
                if stored == operation:
                    connection.commit()
                    return stored
                if stored.state is operation.state:
                    raise RecoveryStoreConflict(
                        "operation state already stores different evidence"
                    )
                connection.execute(
                    """
                    UPDATE harness_recovery_operations
                    SET operation_state = ?, operation_json = ?, updated_at = ?
                    WHERE workspace_id = ? AND operation_id = ?
                    """,
                    (
                        operation.state.value,
                        operation_json,
                        updated_at.isoformat(),
                        workspace_id,
                        operation.operation_id,
                    ),
                )
            connection.commit()
            return operation
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise RecoveryStoreConflict(
                "operation conflicts with durable recovery evidence"
            ) from error
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _save_lease(
        connection: sqlite3.Connection,
        workspace_id: str,
        lease: RecoveryLease,
        updated_at: datetime,
    ) -> RecoveryLease:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """
                SELECT lease_sha256, fencing_generation, expires_at,
                       lease_state, expired_at
                FROM harness_recovery_leases
                WHERE workspace_id = ? AND lease_sha256 = ?
                """,
                (workspace_id, lease.lease_sha256),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO harness_recovery_leases (
                        workspace_id, lease_sha256, fencing_generation,
                        expires_at, lease_state, expired_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        lease.lease_sha256,
                        lease.fencing_generation,
                        lease.expires_at.isoformat(),
                        lease.state.value,
                        (
                            lease.expired_at.isoformat()
                            if lease.expired_at is not None
                            else None
                        ),
                        updated_at.isoformat(),
                    ),
                )
            else:
                stored = decode_lease(row)
                if stored == lease:
                    connection.commit()
                    return stored
                if stored.state is lease.state:
                    raise RecoveryStoreConflict(
                        "lease state already stores different evidence"
                    )
                connection.execute(
                    """
                    UPDATE harness_recovery_leases
                    SET lease_state = ?, expired_at = ?, updated_at = ?
                    WHERE workspace_id = ? AND lease_sha256 = ?
                    """,
                    (
                        lease.state.value,
                        (
                            lease.expired_at.isoformat()
                            if lease.expired_at is not None
                            else None
                        ),
                        updated_at.isoformat(),
                        workspace_id,
                        lease.lease_sha256,
                    ),
                )
            connection.commit()
            return lease
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise RecoveryStoreConflict(
                "lease conflicts with durable recovery evidence"
            ) from error
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("recovery timestamp must use UTC")
