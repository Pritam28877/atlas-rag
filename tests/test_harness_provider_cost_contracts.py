from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ProviderCostAdmissionCode,
    ProviderCostLimits,
    ProviderCostReservation,
    ProviderCostReservationDecision,
    ProviderCostReservationRequest,
    ProviderCostReservationStatus,
)

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)


def request() -> ProviderCostReservationRequest:
    return ProviderCostReservationRequest(
        reservation_id="pcs_" + "1" * 32,
        workspace_id="wsp_" + "2" * 32,
        turn_id="trn_" + "3" * 32,
        request_id="req_" + "4" * 32,
        provider_request_sha256="5" * 64,
        attempt=1,
        estimated_cost_microusd=100,
        limits=ProviderCostLimits(
            max_call_microusd=100,
            max_turn_microusd=500,
            max_workspace_microusd=1_000,
        ),
        requested_at=NOW,
    )


def reservation(
    status: ProviderCostReservationStatus,
    *,
    actual_cost_microusd: int | None = None,
    updated_at: datetime = NOW,
    revision: int = 0,
) -> ProviderCostReservation:
    return ProviderCostReservation(
        request=request(),
        status=status,
        actual_cost_microusd=actual_cost_microusd,
        updated_at=updated_at,
        revision=revision,
    )


def test_cost_limits_require_explicit_positive_hierarchy() -> None:
    with pytest.raises(ValidationError):
        ProviderCostLimits(
            max_call_microusd=0,
            max_turn_microusd=100,
            max_workspace_microusd=100,
        )
    with pytest.raises(ValidationError, match="hierarchical"):
        ProviderCostLimits(
            max_call_microusd=101,
            max_turn_microusd=100,
            max_workspace_microusd=1_000,
        )
    free_request_values = request().model_dump(mode="python")
    free_request_values["estimated_cost_microusd"] = 0
    free_request = ProviderCostReservationRequest.model_validate(
        free_request_values
    )
    assert free_request.estimated_cost_microusd == 0


def test_reservation_lifecycle_is_conservative_and_monotonic() -> None:
    active = reservation(ProviderCostReservationStatus.RESERVED)
    settled = reservation(
        ProviderCostReservationStatus.SETTLED,
        actual_cost_microusd=80,
        updated_at=NOW + timedelta(seconds=1),
        revision=1,
    )

    assert active.actual_cost_microusd is None
    assert settled.actual_cost_microusd == 80
    with pytest.raises(ValidationError, match="exceeds"):
        reservation(
            ProviderCostReservationStatus.SETTLED,
            actual_cost_microusd=101,
            updated_at=NOW + timedelta(seconds=1),
            revision=1,
        )
    with pytest.raises(ValidationError, match="precedes"):
        reservation(
            ProviderCostReservationStatus.RELEASED,
            updated_at=NOW - timedelta(seconds=1),
            revision=1,
        )


def test_admission_decision_cannot_contradict_its_evidence() -> None:
    allowed = ProviderCostReservationDecision(
        allowed=True,
        code=ProviderCostAdmissionCode.ALLOWED,
        reason="Provider cost reservation admitted.",
        reservation=reservation(ProviderCostReservationStatus.RESERVED),
    )

    assert allowed.reservation is not None
    with pytest.raises(ValidationError, match="inconsistent"):
        ProviderCostReservationDecision(
            allowed=False,
            code=ProviderCostAdmissionCode.ALLOWED,
            reason="Contradictory decision.",
        )
    with pytest.raises(ValidationError, match="replay"):
        ProviderCostReservationDecision(
            allowed=False,
            code=ProviderCostAdmissionCode.TURN_LIMIT,
            reason="Turn cap exhausted.",
            already_exists=True,
        )
