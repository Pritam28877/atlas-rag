"""Deterministic fail-closed startup recovery classification."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    OperationRecord,
    OperationState,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.execution import IdempotencyClass

MAXIMUM_RECOVERY_RECORDS = 10_000


class OperationRecoveryAction(StrEnum):
    PRESERVE = "preserve"
    RESUME_PREPARED = "resume_prepared"
    RETRY_IDEMPOTENT = "retry_idempotent"
    NEEDS_OPERATOR = "needs_operator"


class LeaseRecoveryState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


class RecoveryLease(StrictProtocolModel):
    lease_sha256: Sha256
    fencing_generation: int = Field(ge=1, le=2**63 - 1)
    expires_at: UtcTimestamp
    state: LeaseRecoveryState = LeaseRecoveryState.ACTIVE
    expired_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if (self.state is LeaseRecoveryState.EXPIRED) != (
            self.expired_at is not None
        ):
            raise ValueError("expired lease requires expiry evidence")
        if self.expired_at is not None and self.expired_at < self.expires_at:
            raise ValueError("lease cannot expire before its deadline")
        return self


class OperationRecoveryDecision(StrictProtocolModel):
    action: OperationRecoveryAction
    operation: OperationRecord


def classify_operation_recovery(
    operation: OperationRecord,
    *,
    recovered_at: datetime,
) -> OperationRecoveryDecision:
    _require_utc(recovered_at)
    if operation.state is OperationState.PREPARED:
        return OperationRecoveryDecision(
            action=OperationRecoveryAction.RESUME_PREPARED,
            operation=operation,
        )
    if operation.state is OperationState.DISPATCHED:
        if operation.idempotency_class is IdempotencyClass.NON_IDEMPOTENT:
            recovered = OperationRecord.model_validate(
                {
                    **operation.model_dump(),
                    "state": OperationState.AMBIGUOUS,
                    "ambiguous_at": recovered_at,
                    "status_reason": (
                        "Startup found a dispatched non-idempotent operation "
                        "without a durable completion receipt."
                    ),
                }
            )
            return OperationRecoveryDecision(
                action=OperationRecoveryAction.NEEDS_OPERATOR,
                operation=recovered,
            )
        return OperationRecoveryDecision(
            action=OperationRecoveryAction.RETRY_IDEMPOTENT,
            operation=operation,
        )
    if operation.state is OperationState.AMBIGUOUS:
        return OperationRecoveryDecision(
            action=OperationRecoveryAction.NEEDS_OPERATOR,
            operation=operation,
        )
    return OperationRecoveryDecision(
        action=OperationRecoveryAction.PRESERVE,
        operation=operation,
    )


def expire_recovery_lease(
    lease: RecoveryLease,
    *,
    recovered_at: datetime,
) -> RecoveryLease:
    _require_utc(recovered_at)
    if lease.state is LeaseRecoveryState.EXPIRED:
        return lease
    if lease.expires_at > recovered_at:
        return lease
    return RecoveryLease(
        lease_sha256=lease.lease_sha256,
        fencing_generation=lease.fencing_generation,
        expires_at=lease.expires_at,
        state=LeaseRecoveryState.EXPIRED,
        expired_at=recovered_at,
    )


def _require_utc(value: datetime) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        raise ValueError("recovery timestamp must use UTC")
    if offset.total_seconds() != 0:
        raise ValueError("recovery timestamp must use UTC")
