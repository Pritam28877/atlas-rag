"""Transactional immutable command-receipt persistence."""

from __future__ import annotations

import sqlite3
from typing import Never

from app.services.harness.journal.errors import (
    JournalStorageError,
    SessionStoreConflict,
    SessionStoreConflictCode,
)
from app.services.harness.journal.sqlite_session_rows import (
    decode_command_receipt,
    load_command_receipt_row,
)
from app.services.harness.protocol import CommandReplayReceipt


def save_command_receipt(
    connection: sqlite3.Connection,
    receipt: CommandReplayReceipt,
    maximum_receipts: int,
) -> CommandReplayReceipt:
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing_row = load_command_receipt_row(
            connection,
            receipt.workspace_id,
            receipt.idempotency_key,
        )
        if existing_row is not None:
            existing = decode_command_receipt(existing_row)
            _validate_replay(existing, receipt)
            connection.commit()
            return existing
        _require_capacity(connection, receipt.workspace_id, maximum_receipts)
        result_json = receipt.result.model_dump_json()
        connection.execute(
            """
            INSERT INTO harness_command_receipts (
                workspace_id, principal_id, idempotency_key, command_kind,
                request_sha256, response_kind, result_json, result_sha256,
                committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.workspace_id,
                receipt.principal_id,
                receipt.idempotency_key,
                receipt.command_kind.value,
                receipt.request_sha256,
                receipt.response_kind.value,
                result_json,
                receipt.result_sha256,
                receipt.committed_at.isoformat(),
            ),
        )
        stored_row = load_command_receipt_row(
            connection,
            receipt.workspace_id,
            receipt.idempotency_key,
        )
        if stored_row is None:
            raise JournalStorageError("command receipt was not stored")
        stored = decode_command_receipt(stored_row)
        if stored != receipt:
            raise JournalStorageError("stored command receipt differs")
        connection.commit()
        return stored
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise SessionStoreConflict(
            SessionStoreConflictCode.RESULT_CONFLICT
        ) from error
    except BaseException:
        connection.rollback()
        raise


def load_command_receipt(
    connection: sqlite3.Connection,
    workspace_id: str,
    principal_id: str,
    idempotency_key: str,
) -> CommandReplayReceipt | None:
    row = load_command_receipt_row(
        connection,
        workspace_id,
        idempotency_key,
    )
    if row is None:
        return None
    receipt = decode_command_receipt(row)
    _require_owner(receipt.principal_id, principal_id)
    return receipt


def _validate_replay(
    existing: CommandReplayReceipt,
    requested: CommandReplayReceipt,
) -> None:
    _require_owner(existing.principal_id, requested.principal_id)
    if (
        existing.command_kind is not requested.command_kind
        or existing.request_sha256 != requested.request_sha256
    ):
        _reject(SessionStoreConflictCode.IDEMPOTENCY_MISMATCH)
    if existing.response_kind is not requested.response_kind:
        _reject(SessionStoreConflictCode.RESULT_CONFLICT)


def _require_capacity(
    connection: sqlite3.Connection,
    workspace_id: str,
    maximum_receipts: int,
) -> None:
    row = connection.execute(
        """
        SELECT COUNT(*) AS record_count
        FROM harness_command_receipts
        WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None or not isinstance(row["record_count"], int):
        raise JournalStorageError("command receipt count is invalid")
    if row["record_count"] >= maximum_receipts:
        _reject(SessionStoreConflictCode.CAPACITY)


def _require_owner(stored_principal: str, requested_principal: str) -> None:
    if stored_principal != requested_principal:
        _reject(SessionStoreConflictCode.OWNER_MISMATCH)


def _reject(code: SessionStoreConflictCode) -> Never:
    raise SessionStoreConflict(code)
