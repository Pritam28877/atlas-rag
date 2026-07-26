"""O(1) materialized accounting for provider cost reservations."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.services.harness.journal.errors import (
    ProviderCostLedgerConflict,
    ProviderCostLedgerConflictCode,
)
from app.services.harness.protocol import (
    ProviderCostAdmissionCode,
    ProviderCostReservation,
    ProviderCostReservationRequest,
    ProviderCostReservationStatus,
    ProviderCostSnapshot,
)


def admission_denial(
    connection: sqlite3.Connection,
    request: ProviderCostReservationRequest,
) -> ProviderCostAdmissionCode | None:
    estimated = request.estimated_cost_microusd
    if estimated > request.limits.max_call_microusd:
        return ProviderCostAdmissionCode.CALL_LIMIT
    workspace = _workspace_counters(connection, request.workspace_id)
    if (
        workspace["reserved_microusd"]
        + workspace["settled_microusd"]
        + estimated
        > request.limits.max_workspace_microusd
    ):
        return ProviderCostAdmissionCode.WORKSPACE_LIMIT
    turn = _turn_counters(
        connection,
        request.workspace_id,
        request.turn_id,
    )
    if (
        turn["reserved_microusd"]
        + turn["settled_microusd"]
        + estimated
        > request.limits.max_turn_microusd
    ):
        return ProviderCostAdmissionCode.TURN_LIMIT
    return None


def insert_cost_scopes(
    connection: sqlite3.Connection,
    request: ProviderCostReservationRequest,
) -> None:
    requested_at = request.requested_at.isoformat()
    connection.execute(
        """
        INSERT OR IGNORE INTO harness_provider_cost_workspaces (
            workspace_id, updated_at
        ) VALUES (?, ?)
        """,
        (request.workspace_id, requested_at),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO harness_provider_cost_turns (
            workspace_id, turn_id, updated_at
        ) VALUES (?, ?, ?)
        """,
        (request.workspace_id, request.turn_id, requested_at),
    )


def update_cost_counters(
    connection: sqlite3.Connection,
    reservation: ProviderCostReservationRequest,
    *,
    reserved_delta: int,
    settled_delta: int,
    active_delta: int,
    updated_at: datetime,
) -> None:
    timestamp = updated_at.isoformat()
    workspace_update = connection.execute(
        """
        UPDATE harness_provider_cost_workspaces
        SET reserved_microusd = reserved_microusd + ?,
            settled_microusd = settled_microusd + ?,
            active_reservations = active_reservations + ?,
            updated_at = MAX(updated_at, ?)
        WHERE workspace_id = ?
        """,
        (
            reserved_delta,
            settled_delta,
            active_delta,
            timestamp,
            reservation.workspace_id,
        ),
    )
    turn_update = connection.execute(
        """
        UPDATE harness_provider_cost_turns
        SET reserved_microusd = reserved_microusd + ?,
            settled_microusd = settled_microusd + ?,
            active_reservations = active_reservations + ?,
            updated_at = MAX(updated_at, ?)
        WHERE workspace_id = ? AND turn_id = ?
        """,
        (
            reserved_delta,
            settled_delta,
            active_delta,
            timestamp,
            reservation.workspace_id,
            reservation.turn_id,
        ),
    )
    if workspace_update.rowcount != 1 or turn_update.rowcount != 1:
        raise_conflict(ProviderCostLedgerConflictCode.IDENTITY)


def terminal_cost_update(
    connection: sqlite3.Connection,
    reservation: ProviderCostReservation,
    *,
    status: ProviderCostReservationStatus,
    actual_cost_microusd: int | None,
    updated_at: datetime,
) -> None:
    connection.execute(
        """
        UPDATE harness_provider_cost_reservations
        SET reservation_status = ?, actual_cost_microusd = ?,
            updated_at = ?, revision = revision + 1
        WHERE reservation_id = ?
        """,
        (
            status.value,
            actual_cost_microusd,
            updated_at.isoformat(),
            reservation.request.reservation_id,
        ),
    )
    update_cost_counters(
        connection,
        reservation.request,
        reserved_delta=-reservation.request.estimated_cost_microusd,
        settled_delta=actual_cost_microusd or 0,
        active_delta=-1,
        updated_at=updated_at,
    )


def cost_snapshot(
    connection: sqlite3.Connection,
    workspace_id: str,
    turn_id: str,
    observed_at: datetime,
) -> ProviderCostSnapshot:
    workspace = _workspace_counters(connection, workspace_id)
    turn = _turn_counters(connection, workspace_id, turn_id)
    return ProviderCostSnapshot(
        workspace_id=workspace_id,
        turn_id=turn_id,
        workspace_reserved_microusd=workspace["reserved_microusd"],
        workspace_settled_microusd=workspace["settled_microusd"],
        turn_reserved_microusd=turn["reserved_microusd"],
        turn_settled_microusd=turn["settled_microusd"],
        active_reservations=turn["active_reservations"],
        observed_at=observed_at,
    )


def require_cost_identity(
    reservation: ProviderCostReservation,
    provider_request_sha256: str,
) -> None:
    if reservation.request.provider_request_sha256 != provider_request_sha256:
        raise_conflict(ProviderCostLedgerConflictCode.IDENTITY)


def raise_conflict(code: ProviderCostLedgerConflictCode) -> None:
    raise ProviderCostLedgerConflict(code)


def _workspace_counters(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT reserved_microusd, settled_microusd, active_reservations
        FROM harness_provider_cost_workspaces WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    return _counter_values(row)


def _turn_counters(
    connection: sqlite3.Connection,
    workspace_id: str,
    turn_id: str,
) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT reserved_microusd, settled_microusd, active_reservations
        FROM harness_provider_cost_turns
        WHERE workspace_id = ? AND turn_id = ?
        """,
        (workspace_id, turn_id),
    ).fetchone()
    return _counter_values(row)


def _counter_values(row: sqlite3.Row | None) -> dict[str, int]:
    if row is None:
        return {
            "reserved_microusd": 0,
            "settled_microusd": 0,
            "active_reservations": 0,
        }
    return {
        "reserved_microusd": int(row["reserved_microusd"]),
        "settled_microusd": int(row["settled_microusd"]),
        "active_reservations": int(row["active_reservations"]),
    }
