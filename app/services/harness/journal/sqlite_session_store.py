"""Bounded owner for SQLite command replay and event-resume persistence."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.journal.sqlite_session_cursors import (
    load_subscription_cursor,
    purge_expired_subscription_cursors,
    save_subscription_cursor,
)
from app.services.harness.journal.sqlite_session_receipts import (
    load_command_receipt,
    save_command_receipt,
)
from app.services.harness.protocol import (
    CommandReplayReceipt,
    IdempotencyKey,
    PrincipalId,
    SubscriptionCursorRecord,
    SubscriptionId,
    WorkspaceId,
)

MAXIMUM_COMMAND_RECEIPTS_PER_WORKSPACE = 100_000
MAXIMUM_SUBSCRIPTION_CURSORS_PER_WORKSPACE = 65_536
MAXIMUM_CURSOR_PURGE_BATCH = 4_096


class SQLiteSessionStore:
    def __init__(
        self,
        connection_owner: SQLiteConnectionOwner,
        *,
        maximum_command_receipts_per_workspace: int,
        maximum_subscription_cursors_per_workspace: int,
    ) -> None:
        self._connection_owner = connection_owner
        self._maximum_command_receipts = (
            maximum_command_receipts_per_workspace
        )
        self._maximum_subscription_cursors = (
            maximum_subscription_cursors_per_workspace
        )

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 32,
        maximum_command_receipts_per_workspace: int = 10_000,
        maximum_subscription_cursors_per_workspace: int = 1_024,
    ) -> SQLiteSessionStore:
        cls._validate_capacity(
            maximum_command_receipts_per_workspace,
            MAXIMUM_COMMAND_RECEIPTS_PER_WORKSPACE,
            "command receipt",
        )
        cls._validate_capacity(
            maximum_subscription_cursors_per_workspace,
            MAXIMUM_SUBSCRIPTION_CURSORS_PER_WORKSPACE,
            "subscription cursor",
        )
        owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await owner.initialize()
        return cls(
            owner,
            maximum_command_receipts_per_workspace=(
                maximum_command_receipts_per_workspace
            ),
            maximum_subscription_cursors_per_workspace=(
                maximum_subscription_cursors_per_workspace
            ),
        )

    async def close(self) -> None:
        await self._connection_owner.close()

    async def save_command_receipt(
        self,
        receipt: CommandReplayReceipt,
    ) -> CommandReplayReceipt:
        return await self._connection_owner.execute(
            lambda connection: save_command_receipt(
                connection,
                receipt,
                self._maximum_command_receipts,
            )
        )

    async def load_command_receipt(
        self,
        workspace_id: WorkspaceId,
        principal_id: PrincipalId,
        idempotency_key: IdempotencyKey,
    ) -> CommandReplayReceipt | None:
        return await self._connection_owner.execute(
            lambda connection: load_command_receipt(
                connection,
                workspace_id,
                principal_id,
                idempotency_key,
            )
        )

    async def save_subscription_cursor(
        self,
        cursor: SubscriptionCursorRecord,
    ) -> SubscriptionCursorRecord:
        return await self._connection_owner.execute(
            lambda connection: save_subscription_cursor(
                connection,
                cursor,
                self._maximum_subscription_cursors,
            )
        )

    async def load_subscription_cursor(
        self,
        workspace_id: WorkspaceId,
        principal_id: PrincipalId,
        subscription_id: SubscriptionId,
        *,
        observed_at: datetime,
    ) -> SubscriptionCursorRecord | None:
        self._require_utc(observed_at)
        return await self._connection_owner.execute(
            lambda connection: load_subscription_cursor(
                connection,
                workspace_id,
                principal_id,
                subscription_id,
                observed_at,
            )
        )

    async def purge_expired_subscription_cursors(
        self,
        *,
        expired_at_or_before: datetime,
        maximum_records: int = 256,
    ) -> int:
        self._require_utc(expired_at_or_before)
        if not 1 <= maximum_records <= MAXIMUM_CURSOR_PURGE_BATCH:
            raise ValueError("cursor purge batch must be between 1 and 4096")
        return await self._connection_owner.execute(
            lambda connection: purge_expired_subscription_cursors(
                connection,
                expired_at_or_before,
                maximum_records,
            )
        )

    @staticmethod
    def _validate_capacity(value: int, maximum: int, label: str) -> None:
        if not 1 <= value <= maximum:
            raise ValueError(f"{label} capacity must be between 1 and {maximum}")

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("session store timestamp must use UTC")
