"""Transactional immutable request and guarded transition writes."""

import sqlite3

from app.services.harness.journal.errors import (
    ApprovalStoreConflict,
    ApprovalStoreConflictCode,
)
from app.services.harness.journal.sqlite_approval_rows import load_approval
from app.services.harness.protocol.approvals import (
    ApprovalReceipt,
    DurableApprovalRecord,
    DurableApprovalState,
)


def save_approval_request(
    connection: sqlite3.Connection,
    record: DurableApprovalRecord,
    receipt: ApprovalReceipt,
    maximum_records_per_workspace: int,
) -> DurableApprovalRecord:
    _validate_request(record, receipt)
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = load_approval(
            connection,
            record.binding.workspace_id,
            record.binding.approval_id,
        )
        if existing is not None:
            if existing != record:
                raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)
            connection.execute("COMMIT")
            return existing
        record_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM harness_approval_records
            WHERE workspace_id = ?
            """,
            (record.binding.workspace_id,),
        ).fetchone()[0]
        if record_count >= maximum_records_per_workspace:
            raise ApprovalStoreConflict(ApprovalStoreConflictCode.CAPACITY)
        _insert_record(connection, record, receipt.transitioned_at.isoformat())
        _insert_receipt(connection, receipt)
        connection.execute("COMMIT")
        return record
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def transition_approval(
    connection: sqlite3.Connection,
    previous: DurableApprovalRecord,
    updated: DurableApprovalRecord,
    receipt: ApprovalReceipt,
) -> DurableApprovalRecord:
    _validate_transition(previous, updated, receipt)
    connection.execute("BEGIN IMMEDIATE")
    try:
        durable = load_approval(
            connection,
            previous.binding.workspace_id,
            previous.binding.approval_id,
        )
        if durable is None:
            raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)
        if durable != previous:
            raise ApprovalStoreConflict(
                ApprovalStoreConflictCode.CONCURRENT_TRANSITION
            )
        connection.execute(
            """
            UPDATE harness_approval_records
            SET generation = ?, approval_state = ?, record_json = ?, updated_at = ?
            WHERE workspace_id = ? AND approval_id = ?
            """,
            (
                updated.generation,
                updated.state.value,
                updated.model_dump_json(),
                receipt.transitioned_at.isoformat(),
                updated.binding.workspace_id,
                updated.binding.approval_id,
            ),
        )
        _insert_receipt(connection, receipt)
        connection.execute("COMMIT")
        return updated
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _insert_record(
    connection: sqlite3.Connection,
    record: DurableApprovalRecord,
    updated_at: str,
) -> None:
    binding = record.binding
    connection.execute(
        """
        INSERT INTO harness_approval_records (
            workspace_id, approval_id, principal_id, grant_id,
            authorization_sha256, expires_at, generation, approval_state,
            record_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            binding.workspace_id,
            binding.approval_id,
            binding.principal_id,
            binding.grant_id,
            binding.authorization_sha256,
            binding.expires_at.isoformat(),
            record.generation,
            record.state.value,
            record.model_dump_json(),
            updated_at,
        ),
    )


def _insert_receipt(
    connection: sqlite3.Connection,
    receipt: ApprovalReceipt,
) -> None:
    connection.execute(
        """
        INSERT INTO harness_approval_receipts (
            workspace_id, approval_id, generation, receipt_sha256,
            receipt_json, transitioned_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            receipt.workspace_id,
            receipt.approval_id,
            receipt.generation,
            receipt.receipt_sha256,
            receipt.model_dump_json(),
            receipt.transitioned_at.isoformat(),
        ),
    )


def _validate_request(
    record: DurableApprovalRecord,
    receipt: ApprovalReceipt,
) -> None:
    if (
        record.generation != 1
        or record.state is not DurableApprovalState.REQUESTED
        or receipt.generation != 1
        or receipt.state is not DurableApprovalState.REQUESTED
        or receipt.receipt_sha256 != record.latest_receipt_sha256
        or receipt.authorization_sha256
        != record.binding.authorization_sha256
    ):
        raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)


def _validate_transition(
    previous: DurableApprovalRecord,
    updated: DurableApprovalRecord,
    receipt: ApprovalReceipt,
) -> None:
    if (
        updated.binding != previous.binding
        or updated.generation != previous.generation + 1
        or receipt.generation != updated.generation
        or receipt.state is not updated.state
        or receipt.previous_receipt_sha256
        != previous.latest_receipt_sha256
        or receipt.receipt_sha256 != updated.latest_receipt_sha256
        or receipt.authorization_sha256
        != updated.binding.authorization_sha256
    ):
        raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)
