"""Bounded asynchronous facade for the SQLite provider cost ledger."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.journal.sqlite_provider_cost_accounting import (
    cost_snapshot,
)
from app.services.harness.journal.sqlite_provider_cost_reconciliation import (
    release_cost,
    settle_cost,
)
from app.services.harness.journal.sqlite_provider_cost_reservations import (
    reserve_cost,
)
from app.services.harness.protocol import (
    ProviderCostReleaseRequest,
    ProviderCostReservation,
    ProviderCostReservationDecision,
    ProviderCostReservationRequest,
    ProviderCostSettlementRequest,
    ProviderCostSnapshot,
    TurnId,
    WorkspaceId,
)


class SQLiteProviderCostLedger:
    def __init__(self, connection_owner: SQLiteConnectionOwner) -> None:
        self._connection_owner = connection_owner

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 32,
    ) -> SQLiteProviderCostLedger:
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
        request: ProviderCostReservationRequest,
    ) -> ProviderCostReservationDecision:
        return await self._connection_owner.execute(
            lambda connection: reserve_cost(connection, request)
        )

    async def settle(
        self,
        request: ProviderCostSettlementRequest,
    ) -> ProviderCostReservation:
        return await self._connection_owner.execute(
            lambda connection: settle_cost(connection, request)
        )

    async def release(
        self,
        request: ProviderCostReleaseRequest,
    ) -> ProviderCostReservation:
        return await self._connection_owner.execute(
            lambda connection: release_cost(connection, request)
        )

    async def snapshot(
        self,
        workspace_id: WorkspaceId,
        turn_id: TurnId,
        *,
        observed_at: datetime,
    ) -> ProviderCostSnapshot:
        return await self._connection_owner.execute(
            lambda connection: cost_snapshot(
                connection,
                workspace_id,
                turn_id,
                observed_at,
            )
        )
