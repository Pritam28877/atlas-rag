"""Deterministic fail-closed startup recovery classification."""

from __future__ import annotations

from datetime import datetime

from app.services.harness.protocol import (
    OperationRecord,
    OperationState,
)
from app.services.harness.protocol.recovery import (
    LeaseRecoveryState,
    OperationReconciliationDecision,
    OperationReconciliationOutcome,
    OperationRecoveryAction,
    OperationRecoveryDecision,
    OperationStatusProof,
    OperationStatusProofKind,
    RecoveryLease,
)

MAXIMUM_RECOVERY_RECORDS = 10_000


class OperationReconciliationError(ValueError):
    """Stable rejection for absent, stale, or mismatched status proof."""


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
        recovered = OperationRecord.model_validate(
            {
                **operation.model_dump(),
                "state": OperationState.AMBIGUOUS,
                "ambiguous_at": recovered_at,
                "status_reason": (
                    "Startup found a dispatched operation without a durable "
                    "completion receipt or exact status proof."
                ),
            }
        )
        return OperationRecoveryDecision(
            action=OperationRecoveryAction.NEEDS_OPERATOR,
            operation=recovered,
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


def reconcile_ambiguous_operation(
    operation: OperationRecord,
    proof: OperationStatusProof,
) -> OperationReconciliationDecision:
    if operation.state is not OperationState.AMBIGUOUS:
        raise OperationReconciliationError("operation is not ambiguous")
    if (
        proof.operation_id != operation.operation_id
        or proof.tool_name != operation.tool_name
        or proof.tool_version != operation.tool_version
        or proof.args_sha256 != operation.args_sha256
        or proof.fencing_token != operation.lease_fencing_token
        or operation.dispatched_at is None
        or proof.observed_at < operation.dispatched_at
    ):
        raise OperationReconciliationError("operation status proof does not match")
    if proof.kind is OperationStatusProofKind.NOT_STARTED:
        return OperationReconciliationDecision(
            outcome=OperationReconciliationOutcome.RETRY_PROVEN_NOT_STARTED,
            operation=operation,
            proof_sha256=proof.proof_sha256,
        )
    if proof.kind is OperationStatusProofKind.COMPLETED:
        reconciled = OperationRecord.model_validate(
            {
                **operation.model_dump(),
                "state": OperationState.COMPLETED,
                "ambiguous_at": None,
                "terminal_at": proof.observed_at,
                "result_sha256": proof.result_sha256,
                "status_reason": None,
            }
        )
    else:
        reconciled = OperationRecord.model_validate(
            {
                **operation.model_dump(),
                "state": OperationState.FAILED,
                "ambiguous_at": None,
                "terminal_at": proof.observed_at,
                "status_reason": proof.failure_reason,
            }
        )
    return OperationReconciliationDecision(
        outcome=OperationReconciliationOutcome.TERMINAL_PROVEN,
        operation=reconciled,
        proof_sha256=proof.proof_sha256,
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
