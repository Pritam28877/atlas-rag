"""Idempotent provider cost settlement and pre-dispatch release."""

import sqlite3

from app.services.harness.journal.errors import (
    ProviderCostLedgerConflictCode,
)
from app.services.harness.journal.sqlite_provider_cost_accounting import (
    raise_conflict,
    require_cost_identity,
    terminal_cost_update,
)
from app.services.harness.journal.sqlite_provider_cost_rows import (
    required_cost_reservation,
)
from app.services.harness.protocol import (
    ProviderCostReleaseRequest,
    ProviderCostReservation,
    ProviderCostReservationStatus,
    ProviderCostSettlementRequest,
)


def settle_cost(
    connection: sqlite3.Connection,
    request: ProviderCostSettlementRequest,
) -> ProviderCostReservation:
    connection.execute("BEGIN IMMEDIATE")
    try:
        stored = required_cost_reservation(
            connection,
            request.reservation_id,
        )
        require_cost_identity(stored, request.provider_request_sha256)
        if stored.status is ProviderCostReservationStatus.SETTLED:
            if stored.actual_cost_microusd != request.actual_cost_microusd:
                raise_conflict(
                    ProviderCostLedgerConflictCode.RECONCILIATION
                )
            connection.commit()
            return stored
        if stored.status is not ProviderCostReservationStatus.RESERVED:
            raise_conflict(ProviderCostLedgerConflictCode.STATE)
        if (
            request.actual_cost_microusd
            > stored.request.estimated_cost_microusd
            or request.settled_at < stored.updated_at
        ):
            raise_conflict(ProviderCostLedgerConflictCode.RECONCILIATION)
        terminal_cost_update(
            connection,
            stored,
            status=ProviderCostReservationStatus.SETTLED,
            actual_cost_microusd=request.actual_cost_microusd,
            updated_at=request.settled_at,
        )
        updated = required_cost_reservation(
            connection,
            request.reservation_id,
        )
        connection.commit()
        return updated
    except BaseException:
        connection.rollback()
        raise


def release_cost(
    connection: sqlite3.Connection,
    request: ProviderCostReleaseRequest,
) -> ProviderCostReservation:
    connection.execute("BEGIN IMMEDIATE")
    try:
        stored = required_cost_reservation(
            connection,
            request.reservation_id,
        )
        require_cost_identity(stored, request.provider_request_sha256)
        if stored.status is ProviderCostReservationStatus.RELEASED:
            connection.commit()
            return stored
        if (
            stored.status is not ProviderCostReservationStatus.RESERVED
            or request.released_at < stored.updated_at
        ):
            raise_conflict(ProviderCostLedgerConflictCode.STATE)
        terminal_cost_update(
            connection,
            stored,
            status=ProviderCostReservationStatus.RELEASED,
            actual_cost_microusd=None,
            updated_at=request.released_at,
        )
        updated = required_cost_reservation(
            connection,
            request.reservation_id,
        )
        connection.commit()
        return updated
    except BaseException:
        connection.rollback()
        raise
