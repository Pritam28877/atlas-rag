"""Idempotent transactional provider cost admission."""

import sqlite3

from app.services.harness.journal.errors import (
    ProviderCostLedgerConflictCode,
)
from app.services.harness.journal.sqlite_provider_cost_accounting import (
    admission_denial,
    insert_cost_scopes,
    raise_conflict,
    update_cost_counters,
)
from app.services.harness.journal.sqlite_provider_cost_rows import (
    allowed_cost_reservation,
    denied_cost_reservation,
    load_cost_reservation,
    required_cost_reservation,
)
from app.services.harness.protocol import (
    ProviderCostReservationDecision,
    ProviderCostReservationRequest,
    ProviderCostReservationStatus,
)


def reserve_cost(
    connection: sqlite3.Connection,
    request: ProviderCostReservationRequest,
) -> ProviderCostReservationDecision:
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = load_cost_reservation(connection, request.reservation_id)
        if existing is not None:
            if existing.request != request:
                raise_conflict(ProviderCostLedgerConflictCode.IDENTITY)
            if existing.status is not ProviderCostReservationStatus.RESERVED:
                raise_conflict(ProviderCostLedgerConflictCode.STATE)
            connection.commit()
            return allowed_cost_reservation(
                existing,
                already_exists=True,
            )
        duplicate_attempt = connection.execute(
            """
            SELECT 1 FROM harness_provider_cost_reservations
            WHERE workspace_id = ? AND provider_request_sha256 = ?
                AND attempt = ?
            """,
            (
                request.workspace_id,
                request.provider_request_sha256,
                request.attempt,
            ),
        ).fetchone()
        if duplicate_attempt is not None:
            raise_conflict(ProviderCostLedgerConflictCode.IDENTITY)
        denial = admission_denial(connection, request)
        if denial is not None:
            connection.commit()
            return denied_cost_reservation(denial)
        insert_cost_scopes(connection, request)
        connection.execute(
            """
            INSERT INTO harness_provider_cost_reservations (
                reservation_id, workspace_id, turn_id,
                provider_request_sha256, attempt,
                estimated_cost_microusd, request_json,
                reservation_status, requested_at, updated_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, 0)
            """,
            (
                request.reservation_id,
                request.workspace_id,
                request.turn_id,
                request.provider_request_sha256,
                request.attempt,
                request.estimated_cost_microusd,
                request.model_dump_json(),
                request.requested_at.isoformat(),
                request.requested_at.isoformat(),
            ),
        )
        update_cost_counters(
            connection,
            request,
            reserved_delta=request.estimated_cost_microusd,
            settled_delta=0,
            active_delta=1,
            updated_at=request.requested_at,
        )
        stored = required_cost_reservation(
            connection,
            request.reservation_id,
        )
        connection.commit()
        return allowed_cost_reservation(stored)
    except BaseException:
        connection.rollback()
        raise
