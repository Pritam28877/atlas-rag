"""Atomic operation state and synchronous admission-receipt persistence."""

import sqlite3
from datetime import datetime

from app.services.harness.journal.errors import RecoveryStoreConflict
from app.services.harness.journal.sqlite_recovery_rows import decode_operation
from app.services.harness.protocol import OperationRecord, OperationState
from app.services.harness.protocol.operation_admission import (
    OperationDurabilityReceipt,
    operation_record_sha256,
)


def save_operation(
    connection: sqlite3.Connection,
    workspace_id: str,
    operation: OperationRecord,
    updated_at: datetime,
) -> OperationRecord:
    connection.execute("BEGIN IMMEDIATE")
    try:
        durable = _upsert_operation(
            connection,
            workspace_id,
            operation,
            updated_at,
        )
        connection.commit()
        return durable
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise RecoveryStoreConflict(
            "operation conflicts with durable recovery evidence"
        ) from error
    except BaseException:
        connection.rollback()
        raise


def save_operation_admission(
    connection: sqlite3.Connection,
    workspace_id: str,
    operation: OperationRecord,
    receipt: OperationDurabilityReceipt,
    updated_at: datetime,
) -> tuple[OperationRecord, OperationDurabilityReceipt]:
    _validate_admission(workspace_id, operation, receipt, updated_at)
    connection.execute("BEGIN IMMEDIATE")
    try:
        durable = _upsert_operation(
            connection,
            workspace_id,
            operation,
            updated_at,
        )
        _insert_admission_receipt(connection, receipt)
        connection.commit()
        return durable, receipt
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise RecoveryStoreConflict(
            "operation admission conflicts with durable evidence"
        ) from error
    except BaseException:
        connection.rollback()
        raise


def load_operation_admission_receipts(
    connection: sqlite3.Connection,
    workspace_id: str,
    operation_id: str,
) -> tuple[OperationDurabilityReceipt, ...]:
    rows = connection.execute(
        """
        SELECT operation_state, fencing_token, receipt_sha256,
            receipt_json, durable_at
        FROM harness_operation_admission_receipts
        WHERE workspace_id = ? AND operation_id = ?
        ORDER BY CASE operation_state
            WHEN 'prepared' THEN 1
            WHEN 'dispatched' THEN 2
        END
        """,
        (workspace_id, operation_id),
    ).fetchall()
    receipts: list[OperationDurabilityReceipt] = []
    previous_sha256 = None
    for row in rows:
        receipt = OperationDurabilityReceipt.model_validate_json(
            row["receipt_json"]
        )
        if (
            receipt.state.value != row["operation_state"]
            or receipt.fencing_token != row["fencing_token"]
            or receipt.receipt_sha256 != row["receipt_sha256"]
            or receipt.durable_at.isoformat() != row["durable_at"]
            or receipt.previous_receipt_sha256 != previous_sha256
        ):
            raise RecoveryStoreConflict(
                "operation admission receipt evidence is invalid"
            )
        receipts.append(receipt)
        previous_sha256 = receipt.receipt_sha256
    return tuple(receipts)


def _upsert_operation(
    connection: sqlite3.Connection,
    workspace_id: str,
    operation: OperationRecord,
    updated_at: datetime,
) -> OperationRecord:
    row = connection.execute(
        """
        SELECT operation_id, operation_state, idempotency_class, operation_json
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
                operation.model_dump_json(),
                updated_at.isoformat(),
            ),
        )
        return operation
    stored = decode_operation(row)
    if stored == operation:
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
            operation.model_dump_json(),
            updated_at.isoformat(),
            workspace_id,
            operation.operation_id,
        ),
    )
    return operation


def _insert_admission_receipt(
    connection: sqlite3.Connection,
    receipt: OperationDurabilityReceipt,
) -> None:
    if receipt.state is OperationState.DISPATCHED:
        prepared = connection.execute(
            """
            SELECT receipt_sha256
            FROM harness_operation_admission_receipts
            WHERE workspace_id = ? AND operation_id = ?
              AND operation_state = 'prepared'
            """,
            (receipt.workspace_id, receipt.operation_id),
        ).fetchone()
        if (
            prepared is None
            or prepared["receipt_sha256"]
            != receipt.previous_receipt_sha256
        ):
            raise RecoveryStoreConflict(
                "dispatch receipt lacks exact prepared evidence"
            )
    existing = connection.execute(
        """
        SELECT receipt_json
        FROM harness_operation_admission_receipts
        WHERE workspace_id = ? AND operation_id = ? AND operation_state = ?
        """,
        (receipt.workspace_id, receipt.operation_id, receipt.state.value),
    ).fetchone()
    if existing is not None:
        stored = OperationDurabilityReceipt.model_validate_json(
            existing["receipt_json"]
        )
        if stored != receipt:
            raise RecoveryStoreConflict(
                "operation admission receipt already differs"
            )
        return
    connection.execute(
        """
        INSERT INTO harness_operation_admission_receipts (
            workspace_id, operation_id, operation_state, fencing_token,
            receipt_sha256, receipt_json, durable_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt.workspace_id,
            receipt.operation_id,
            receipt.state.value,
            receipt.fencing_token,
            receipt.receipt_sha256,
            receipt.model_dump_json(),
            receipt.durable_at.isoformat(),
        ),
    )


def _validate_admission(
    workspace_id: str,
    operation: OperationRecord,
    receipt: OperationDurabilityReceipt,
    updated_at: datetime,
) -> None:
    if (
        receipt.workspace_id != workspace_id
        or receipt.operation_id != operation.operation_id
        or receipt.state is not operation.state
        or receipt.fencing_token != operation.lease_fencing_token
        or receipt.operation_sha256 != operation_record_sha256(operation)
        or receipt.durable_at != updated_at
    ):
        raise RecoveryStoreConflict("operation admission evidence does not match")
