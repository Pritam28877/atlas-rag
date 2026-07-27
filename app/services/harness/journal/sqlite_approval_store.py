"""Bounded asynchronous facade for durable approval persistence."""

from __future__ import annotations

from pathlib import Path

from app.services.harness.journal.sqlite_approval_rows import (
    load_approval_receipts,
    load_owned_approval,
)
from app.services.harness.journal.sqlite_approval_writes import (
    save_approval_request,
    transition_approval,
)
from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.protocol import ApprovalId, PrincipalId, WorkspaceId
from app.services.harness.protocol.approvals import (
    ApprovalReceipt,
    DurableApprovalRecord,
)

MAXIMUM_APPROVALS_PER_WORKSPACE = 100_000


class SQLiteApprovalStore:
    def __init__(
        self,
        connection_owner: SQLiteConnectionOwner,
        *,
        maximum_records_per_workspace: int,
    ) -> None:
        self._connection_owner = connection_owner
        self._maximum_records_per_workspace = maximum_records_per_workspace

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 32,
        maximum_records_per_workspace: int = 10_000,
    ) -> SQLiteApprovalStore:
        if not 1 <= maximum_records_per_workspace <= (
            MAXIMUM_APPROVALS_PER_WORKSPACE
        ):
            raise ValueError("approval capacity must be between 1 and 100000")
        owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await owner.initialize()
        return cls(
            owner,
            maximum_records_per_workspace=maximum_records_per_workspace,
        )

    async def close(self) -> None:
        await self._connection_owner.close()

    async def save_request(
        self,
        record: DurableApprovalRecord,
        receipt: ApprovalReceipt,
    ) -> DurableApprovalRecord:
        return await self._connection_owner.execute(
            lambda connection: save_approval_request(
                connection,
                record,
                receipt,
                self._maximum_records_per_workspace,
            )
        )

    async def transition(
        self,
        previous: DurableApprovalRecord,
        updated: DurableApprovalRecord,
        receipt: ApprovalReceipt,
    ) -> DurableApprovalRecord:
        return await self._connection_owner.execute(
            lambda connection: transition_approval(
                connection,
                previous,
                updated,
                receipt,
            )
        )

    async def load(
        self,
        workspace_id: WorkspaceId,
        principal_id: PrincipalId,
        approval_id: ApprovalId,
    ) -> DurableApprovalRecord | None:
        return await self._connection_owner.execute(
            lambda connection: load_owned_approval(
                connection,
                workspace_id,
                principal_id,
                approval_id,
            )
        )

    async def receipts(
        self,
        workspace_id: WorkspaceId,
        principal_id: PrincipalId,
        approval_id: ApprovalId,
    ) -> tuple[ApprovalReceipt, ...]:
        return await self._connection_owner.execute(
            lambda connection: load_approval_receipts(
                connection,
                workspace_id,
                principal_id,
                approval_id,
            )
        )
