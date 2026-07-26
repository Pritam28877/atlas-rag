"""Strict decoding for durable command receipts and subscription cursors."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from typing import cast

from pydantic import TypeAdapter, ValidationError

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.protocol import (
    CommandKind,
    CommandReplayReceipt,
    CommandResponseKind,
    Payload,
    SubscriptionCursorRecord,
)

PAYLOAD_ADAPTER: TypeAdapter[Payload] = TypeAdapter(Payload)


def load_command_receipt_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    idempotency_key: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT workspace_id, principal_id, idempotency_key, command_kind,
               request_sha256, response_kind, result_json, result_sha256,
               committed_at
        FROM harness_command_receipts
        WHERE workspace_id = ? AND idempotency_key = ?
        """,
        (workspace_id, idempotency_key),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def decode_command_receipt(row: sqlite3.Row) -> CommandReplayReceipt:
    try:
        result_json = _text(row, "result_json")
        stored_result_sha256 = _text(row, "result_sha256")
        actual_result_sha256 = hashlib.sha256(result_json.encode()).hexdigest()
        if actual_result_sha256 != stored_result_sha256:
            raise ValueError("stored command result digest differs")
        result = PAYLOAD_ADAPTER.validate_json(result_json, strict=True)
        return CommandReplayReceipt(
            workspace_id=_text(row, "workspace_id"),
            principal_id=_text(row, "principal_id"),
            idempotency_key=_text(row, "idempotency_key"),
            command_kind=CommandKind(_text(row, "command_kind")),
            request_sha256=_text(row, "request_sha256"),
            response_kind=CommandResponseKind(_text(row, "response_kind")),
            result=result,
            result_sha256=stored_result_sha256,
            committed_at=_timestamp(row, "committed_at"),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError("stored command receipt is invalid") from error


def load_subscription_cursor_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    subscription_id: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT workspace_id, principal_id, subscription_id, generation,
               acknowledged_sequence, delivered_sequence, updated_at,
               expires_at
        FROM harness_subscription_cursors
        WHERE workspace_id = ? AND subscription_id = ?
        """,
        (workspace_id, subscription_id),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def decode_subscription_cursor(row: sqlite3.Row) -> SubscriptionCursorRecord:
    try:
        return SubscriptionCursorRecord(
            workspace_id=_text(row, "workspace_id"),
            principal_id=_text(row, "principal_id"),
            subscription_id=_text(row, "subscription_id"),
            generation=_integer(row, "generation"),
            acknowledged_sequence=_integer(row, "acknowledged_sequence"),
            delivered_sequence=_integer(row, "delivered_sequence"),
            updated_at=_timestamp(row, "updated_at"),
            expires_at=_timestamp(row, "expires_at"),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise JournalStorageError(
            "stored subscription cursor is invalid"
        ) from error


def _integer(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def _timestamp(row: sqlite3.Row, field: str) -> datetime:
    return datetime.fromisoformat(_text(row, field))
