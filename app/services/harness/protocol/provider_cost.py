"""Provider cost admission, reservation, and reconciliation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    ProviderCostReservationId,
    RequestId,
    Sha256,
    StrictProtocolModel,
    TurnId,
    UtcTimestamp,
    WorkspaceId,
)

MAXIMUM_PROVIDER_COST_MICROUSD = 10_000_000_000


class ProviderCostLimits(StrictProtocolModel):
    max_call_microusd: int = Field(
        ge=1,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    max_turn_microusd: int = Field(
        ge=1,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    max_workspace_microusd: int = Field(
        ge=1,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )

    @model_validator(mode="after")
    def validate_hierarchy(self) -> Self:
        if not (
            self.max_call_microusd
            <= self.max_turn_microusd
            <= self.max_workspace_microusd
        ):
            raise ValueError("provider cost limits must be hierarchical")
        return self


class ProviderCostReservationRequest(StrictProtocolModel):
    reservation_id: ProviderCostReservationId
    workspace_id: WorkspaceId
    turn_id: TurnId
    request_id: RequestId
    provider_request_sha256: Sha256
    attempt: int = Field(ge=1, le=64)
    estimated_cost_microusd: int = Field(
        ge=0,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    limits: ProviderCostLimits
    requested_at: UtcTimestamp


class ProviderCostReservationStatus(StrEnum):
    RESERVED = "reserved"
    RELEASED = "released"
    SETTLED = "settled"


class ProviderCostReservation(StrictProtocolModel):
    request: ProviderCostReservationRequest
    status: ProviderCostReservationStatus
    actual_cost_microusd: int | None = Field(
        default=None,
        ge=0,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    updated_at: UtcTimestamp
    revision: int = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        settled = self.status is ProviderCostReservationStatus.SETTLED
        if settled != (self.actual_cost_microusd is not None):
            raise ValueError("settled provider cost requires actual cost")
        if (
            self.actual_cost_microusd is not None
            and self.actual_cost_microusd
            > self.request.estimated_cost_microusd
        ):
            raise ValueError("actual provider cost exceeds its reservation")
        if self.updated_at < self.request.requested_at:
            raise ValueError("provider cost update precedes reservation")
        if self.status is ProviderCostReservationStatus.RESERVED:
            if self.revision != 0:
                raise ValueError("active provider reservation must be revision zero")
        elif self.revision < 1:
            raise ValueError("terminal provider reservation requires a revision")
        return self


class ProviderCostAdmissionCode(StrEnum):
    ALLOWED = "allowed"
    CALL_LIMIT = "call_limit"
    TURN_LIMIT = "turn_limit"
    WORKSPACE_LIMIT = "workspace_limit"


class ProviderCostReservationDecision(StrictProtocolModel):
    allowed: bool
    code: ProviderCostAdmissionCode
    reason: BoundedReason
    reservation: ProviderCostReservation | None = None
    already_exists: bool = False

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.allowed != (self.reservation is not None):
            raise ValueError("provider cost admission result is inconsistent")
        if self.allowed != (self.code is ProviderCostAdmissionCode.ALLOWED):
            raise ValueError("provider cost admission code is inconsistent")
        if self.already_exists and self.reservation is None:
            raise ValueError("provider cost replay requires a reservation")
        return self


class ProviderCostSettlementRequest(StrictProtocolModel):
    reservation_id: ProviderCostReservationId
    provider_request_sha256: Sha256
    actual_cost_microusd: int = Field(
        ge=0,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    settled_at: UtcTimestamp


class ProviderCostReleaseRequest(StrictProtocolModel):
    reservation_id: ProviderCostReservationId
    provider_request_sha256: Sha256
    released_at: UtcTimestamp


class ProviderCostSnapshot(StrictProtocolModel):
    workspace_id: WorkspaceId
    turn_id: TurnId
    workspace_reserved_microusd: int = Field(
        ge=0,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    workspace_settled_microusd: int = Field(
        ge=0,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    turn_reserved_microusd: int = Field(
        ge=0,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    turn_settled_microusd: int = Field(
        ge=0,
        le=MAXIMUM_PROVIDER_COST_MICROUSD,
    )
    active_reservations: int = Field(ge=0, le=1_000_000)
    observed_at: UtcTimestamp


class ProviderCostLedger(Protocol):
    async def reserve(
        self,
        request: ProviderCostReservationRequest,
    ) -> ProviderCostReservationDecision: ...

    async def settle(
        self,
        request: ProviderCostSettlementRequest,
    ) -> ProviderCostReservation: ...

    async def release(
        self,
        request: ProviderCostReleaseRequest,
    ) -> ProviderCostReservation: ...

    async def snapshot(
        self,
        workspace_id: WorkspaceId,
        turn_id: TurnId,
        *,
        observed_at: UtcTimestamp,
    ) -> ProviderCostSnapshot: ...
