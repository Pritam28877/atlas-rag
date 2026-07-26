"""Deterministic fail-closed startup recovery classification."""

from __future__ import annotations

from datetime import datetime

from app.services.harness.protocol import (
    OperationRecord,
    OperationState,
)
from app.services.harness.protocol.execution import IdempotencyClass
from app.services.harness.protocol.recovery import (
    LeaseRecoveryState,
    OperationRecoveryAction,
    OperationRecoveryDecision,
    RecoveryLease,
)

MAXIMUM_RECOVERY_RECORDS = 10_000


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
