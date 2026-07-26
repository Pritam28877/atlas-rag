"""Strict contracts for bounded cancellation, containment, and evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    Sha256,
    StrictProtocolModel,
    TurnId,
    UtcTimestamp,
)

MAXIMUM_OWNED_CANCELLATION_RESOURCES = 256


class CancellationResourceKind(StrEnum):
    PROCESS_TREE = "process_tree"
    PROVIDER_REQUEST = "provider_request"


class CancellationControlOutcome(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class CancellationSettlement(StrEnum):
    COOPERATIVE = "cooperative"
    AFTER_ESCALATION = "after_escalation"
    CONTAINMENT_FAILED = "containment_failed"


class CancellationScopeState(StrEnum):
    OPEN = "open"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    NEEDS_OPERATOR = "needs_operator"


class CancellationResourceIdentity(StrictProtocolModel):
    resource_id: Sha256
    kind: CancellationResourceKind


class CancellationResourceResult(StrictProtocolModel):
    identity: CancellationResourceIdentity
    cancel_request: CancellationControlOutcome
    force_request: CancellationControlOutcome
    settlement: CancellationSettlement

    @model_validator(mode="after")
    def validate_attempts(self) -> Self:
        force_attempted = (
            self.force_request is not CancellationControlOutcome.NOT_ATTEMPTED
        )
        if self.cancel_request is CancellationControlOutcome.NOT_ATTEMPTED:
            raise ValueError("resource cancellation must be requested")
        if (
            self.settlement is CancellationSettlement.COOPERATIVE
            and force_attempted
        ):
            raise ValueError("cooperative settlement cannot include escalation")
        if (
            self.settlement is not CancellationSettlement.COOPERATIVE
            and not force_attempted
        ):
            raise ValueError("unsettled resource requires force escalation")
        return self


class CancellationReport(StrictProtocolModel):
    turn_id: TurnId
    reason: BoundedReason
    state: CancellationScopeState
    requested_at: UtcTimestamp
    deadline_at: UtcTimestamp
    completed_at: UtcTimestamp
    deadline_exceeded: bool
    resources: tuple[CancellationResourceResult, ...] = Field(
        max_length=MAXIMUM_OWNED_CANCELLATION_RESOURCES
    )

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if not self.requested_at < self.deadline_at:
            raise ValueError("cancellation deadline must follow request")
        if self.completed_at < self.requested_at:
            raise ValueError("cancellation completion precedes request")
        if self.deadline_exceeded != (self.completed_at > self.deadline_at):
            raise ValueError("deadline flag disagrees with completion")
        identities = tuple(
            (result.identity.kind.value, result.identity.resource_id)
            for result in self.resources
        )
        if tuple(sorted(set(identities))) != identities:
            raise ValueError("cancellation resources must be unique and sorted")
        containment_failed = any(
            result.settlement is CancellationSettlement.CONTAINMENT_FAILED
            for result in self.resources
        )
        expected_state = (
            CancellationScopeState.NEEDS_OPERATOR
            if containment_failed or self.deadline_exceeded
            else CancellationScopeState.CANCELLED
        )
        if self.state is not expected_state:
            raise ValueError("cancellation state disagrees with outcomes")
        return self


class OwnedCancellationResource(Protocol):
    """Adapter controls are non-blocking; settlement waits are cancellation-safe."""

    @property
    def cancellation_identity(self) -> CancellationResourceIdentity: ...

    def request_cancel(self) -> None: ...

    def force_terminate(self) -> None: ...

    async def wait_settled(self) -> None: ...


class CancellationEvidenceSink(Protocol):
    """Evidence writes must be cancellation-safe and adapter-bounded."""

    async def record_cancellation(self, report: CancellationReport) -> None: ...
