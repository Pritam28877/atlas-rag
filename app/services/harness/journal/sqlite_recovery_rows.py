"""Strict bounded decoding for SQLite startup recovery rows."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pydantic import ValidationError

from app.services.harness.journal.errors import (
    JournalStorageError,
    RecoveryStoreConflict,
)
from app.services.harness.protocol import OperationRecord
from app.services.harness.protocol.recovery import (
    LeaseRecoveryState,
    RecoveryLease,
)

MAXIMUM_RECOVERY_RECORDS = 10_000


def load_recoverable_operations(
    connection: sqlite3.Connection,
    workspace_id: str,
    maximum_records: int,
) -> tuple[OperationRecord, ...]:
    _validate_limit(maximum_records)
    rows = connection.execute(
        """
        SELECT operation_json
        FROM harness_recovery_operations
        WHERE workspace_id = ?
          AND operation_state IN ('prepared', 'dispatched', 'ambiguous')
        ORDER BY updated_at, operation_id
        LIMIT ?
        """,
        (workspace_id, maximum_records + 1),
    ).fetchall()
    if len(rows) > maximum_records:
        raise RecoveryStoreConflict("recovery operation limit exceeded")
    try:
        return tuple(
            OperationRecord.model_validate_json(row["operation_json"])
            for row in rows
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("recovery operation evidence is invalid") from error


def load_active_leases(
    connection: sqlite3.Connection,
    workspace_id: str,
    maximum_records: int,
) -> tuple[RecoveryLease, ...]:
    _validate_limit(maximum_records)
    rows = connection.execute(
        """
        SELECT lease_sha256, fencing_generation, expires_at,
               lease_state, expired_at
        FROM harness_recovery_leases
        WHERE workspace_id = ? AND lease_state = 'active'
        ORDER BY expires_at, lease_sha256
        LIMIT ?
        """,
        (workspace_id, maximum_records + 1),
    ).fetchall()
    if len(rows) > maximum_records:
        raise RecoveryStoreConflict("recovery lease limit exceeded")
    try:
        return tuple(_decode_lease(row) for row in rows)
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("recovery lease evidence is invalid") from error


def decode_operation(row: sqlite3.Row) -> OperationRecord:
    try:
        operation = OperationRecord.model_validate_json(row["operation_json"])
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("recovery operation evidence is invalid") from error
    if (
        operation.operation_id != row["operation_id"]
        or operation.state.value != row["operation_state"]
        or operation.idempotency_class.value != row["idempotency_class"]
    ):
        raise JournalStorageError("recovery operation columns disagree")
    return operation


def load_operation(
    connection: sqlite3.Connection,
    workspace_id: str,
    operation_id: str,
) -> OperationRecord | None:
    row = connection.execute(
        """
        SELECT operation_id, operation_state, idempotency_class, operation_json
        FROM harness_recovery_operations
        WHERE workspace_id = ? AND operation_id = ?
        """,
        (workspace_id, operation_id),
    ).fetchone()
    return decode_operation(row) if row is not None else None


def decode_lease(row: sqlite3.Row) -> RecoveryLease:
    try:
        return _decode_lease(row)
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("recovery lease evidence is invalid") from error


def _decode_lease(row: sqlite3.Row) -> RecoveryLease:
    return RecoveryLease(
        lease_sha256=row["lease_sha256"],
        fencing_generation=row["fencing_generation"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
        state=LeaseRecoveryState(row["lease_state"]),
        expired_at=(
            datetime.fromisoformat(row["expired_at"])
            if row["expired_at"] is not None
            else None
        ),
    )


def _validate_limit(maximum_records: int) -> None:
    if not 1 <= maximum_records <= MAXIMUM_RECOVERY_RECORDS:
        raise ValueError("recovery record limit must be between 1 and 10000")
