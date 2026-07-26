"""Typed row loading and decisions for the SQLite provider cost ledger."""

import sqlite3
from datetime import datetime

from app.services.harness.journal.errors import (
    ProviderCostLedgerConflict,
    ProviderCostLedgerConflictCode,
)
from app.services.harness.protocol import (
    ProviderCostAdmissionCode,
    ProviderCostReservation,
    ProviderCostReservationDecision,
    ProviderCostReservationRequest,
    ProviderCostReservationStatus,
)


def load_cost_reservation(
    connection: sqlite3.Connection,
    reservation_id: str,
) -> ProviderCostReservation | None:
    row = connection.execute(
        """
        SELECT request_json, reservation_status, actual_cost_microusd,
            updated_at, revision
        FROM harness_provider_cost_reservations
        WHERE reservation_id = ?
        """,
        (reservation_id,),
    ).fetchone()
    if row is None:
        return None
    request = ProviderCostReservationRequest.model_validate_json(
        row["request_json"]
    )
    return ProviderCostReservation(
        request=request,
        status=ProviderCostReservationStatus(row["reservation_status"]),
        actual_cost_microusd=row["actual_cost_microusd"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
        revision=row["revision"],
    )


def required_cost_reservation(
    connection: sqlite3.Connection,
    reservation_id: str,
) -> ProviderCostReservation:
    reservation = load_cost_reservation(connection, reservation_id)
    if reservation is None:
        raise ProviderCostLedgerConflict(
            ProviderCostLedgerConflictCode.IDENTITY
        )
    return reservation


def allowed_cost_reservation(
    reservation: ProviderCostReservation,
    *,
    already_exists: bool = False,
) -> ProviderCostReservationDecision:
    return ProviderCostReservationDecision(
        allowed=True,
        code=ProviderCostAdmissionCode.ALLOWED,
        reason="Provider cost reservation admitted.",
        reservation=reservation,
        already_exists=already_exists,
    )


def denied_cost_reservation(
    code: ProviderCostAdmissionCode,
) -> ProviderCostReservationDecision:
    reasons = {
        ProviderCostAdmissionCode.CALL_LIMIT: (
            "Provider call cost cap exhausted."
        ),
        ProviderCostAdmissionCode.TURN_LIMIT: (
            "Provider turn cost cap exhausted."
        ),
        ProviderCostAdmissionCode.WORKSPACE_LIMIT: (
            "Provider workspace cost cap exhausted."
        ),
    }
    return ProviderCostReservationDecision(
        allowed=False,
        code=code,
        reason=reasons[code],
    )
