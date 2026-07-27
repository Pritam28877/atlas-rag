"""Typed row loading and receipt-chain validation for SQLite approvals."""

import sqlite3

from app.services.harness.journal.errors import (
    ApprovalStoreConflict,
    ApprovalStoreConflictCode,
)
from app.services.harness.protocol import ApprovalId, PrincipalId, WorkspaceId
from app.services.harness.protocol.approvals import (
    ApprovalReceipt,
    DurableApprovalRecord,
)

MAXIMUM_APPROVAL_RECEIPTS = 16


def load_approval(
    connection: sqlite3.Connection,
    workspace_id: WorkspaceId,
    approval_id: ApprovalId,
) -> DurableApprovalRecord | None:
    row = connection.execute(
        """
        SELECT principal_id, grant_id, authorization_sha256, expires_at,
            generation, approval_state, record_json
        FROM harness_approval_records
        WHERE workspace_id = ? AND approval_id = ?
        """,
        (workspace_id, approval_id),
    ).fetchone()
    if row is None:
        return None
    record = DurableApprovalRecord.model_validate_json(row["record_json"])
    binding = record.binding
    if (
        binding.workspace_id != workspace_id
        or binding.approval_id != approval_id
        or binding.principal_id != row["principal_id"]
        or binding.grant_id != row["grant_id"]
        or binding.authorization_sha256 != row["authorization_sha256"]
        or binding.expires_at.isoformat() != row["expires_at"]
        or record.generation != row["generation"]
        or record.state.value != row["approval_state"]
    ):
        raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)
    return record


def load_owned_approval(
    connection: sqlite3.Connection,
    workspace_id: WorkspaceId,
    principal_id: PrincipalId,
    approval_id: ApprovalId,
) -> DurableApprovalRecord | None:
    record = load_approval(connection, workspace_id, approval_id)
    if record is None:
        return None
    if record.binding.principal_id != principal_id:
        raise ApprovalStoreConflict(ApprovalStoreConflictCode.OWNER)
    return record


def load_approval_receipts(
    connection: sqlite3.Connection,
    workspace_id: WorkspaceId,
    principal_id: PrincipalId,
    approval_id: ApprovalId,
) -> tuple[ApprovalReceipt, ...]:
    record = load_owned_approval(
        connection,
        workspace_id,
        principal_id,
        approval_id,
    )
    if record is None:
        return ()
    rows = connection.execute(
        """
        SELECT generation, receipt_sha256, transitioned_at, receipt_json
        FROM harness_approval_receipts
        WHERE workspace_id = ? AND approval_id = ?
        ORDER BY generation ASC
        LIMIT ?
        """,
        (workspace_id, approval_id, MAXIMUM_APPROVAL_RECEIPTS),
    ).fetchall()
    receipts_list: list[ApprovalReceipt] = []
    for row in rows:
        receipt = ApprovalReceipt.model_validate_json(row["receipt_json"])
        if (
            receipt.generation != row["generation"]
            or receipt.receipt_sha256 != row["receipt_sha256"]
            or receipt.transitioned_at.isoformat() != row["transitioned_at"]
        ):
            raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)
        receipts_list.append(receipt)
    receipts = tuple(receipts_list)
    _validate_receipt_chain(record, receipts)
    return receipts


def _validate_receipt_chain(
    record: DurableApprovalRecord,
    receipts: tuple[ApprovalReceipt, ...],
) -> None:
    if len(receipts) != record.generation:
        raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)
    previous_sha256 = None
    for expected_generation, receipt in enumerate(receipts, start=1):
        if (
            receipt.generation != expected_generation
            or receipt.previous_receipt_sha256 != previous_sha256
            or receipt.authorization_sha256
            != record.binding.authorization_sha256
        ):
            raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)
        previous_sha256 = receipt.receipt_sha256
    if previous_sha256 != record.latest_receipt_sha256:
        raise ApprovalStoreConflict(ApprovalStoreConflictCode.IDENTITY)
