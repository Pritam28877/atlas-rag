from datetime import UTC, datetime, timedelta

from app.services.harness.protocol import (
    IdempotencyClass,
    OperationLimits,
    OperationRecord,
    OperationState,
)
from app.services.harness.runtime import (
    LeaseRecoveryState,
    OperationRecoveryAction,
    RecoveryLease,
    classify_operation_recovery,
    expire_recovery_lease,
)

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def operation(
    state: OperationState,
    idempotency_class: IdempotencyClass,
) -> OperationRecord:
    values: dict[str, object] = {
        "operation_id": identifier("opn"),
        "turn_id": identifier("trn"),
        "idempotency_key": "recovery-operation-0001",
        "idempotency_class": idempotency_class,
        "attempt": 1,
        "lease_fencing_token": 1,
        "tool_name": "workspace.write_file",
        "tool_version": "1.0.0",
        "args_sha256": "a" * 64,
        "capability": "filesystem.write",
        "policy_decision_id": identifier("dcs"),
        "state": state,
        "limits": OperationLimits(
            max_duration_ms=1000,
            max_cpu_ms=1000,
            max_memory_bytes=16 * 1024 * 1024,
            max_output_bytes=1024,
            max_processes=1,
        ),
        "prepared_at": NOW,
    }
    if state is not OperationState.PREPARED:
        values["dispatched_at"] = NOW + timedelta(seconds=1)
    return OperationRecord.model_validate(values)


def test_dispatched_non_idempotent_operation_needs_operator() -> None:
    decision = classify_operation_recovery(
        operation(
            OperationState.DISPATCHED,
            IdempotencyClass.NON_IDEMPOTENT,
        ),
        recovered_at=NOW + timedelta(seconds=2),
    )
    repeated = classify_operation_recovery(
        decision.operation,
        recovered_at=NOW + timedelta(seconds=3),
    )

    assert decision.action is OperationRecoveryAction.NEEDS_OPERATOR
    assert decision.operation.state is OperationState.AMBIGUOUS
    assert decision.operation.ambiguous_at == NOW + timedelta(seconds=2)
    assert repeated == decision


def test_prepared_and_repeatable_operations_are_safe_to_resume() -> None:
    prepared = classify_operation_recovery(
        operation(OperationState.PREPARED, IdempotencyClass.NON_IDEMPOTENT),
        recovered_at=NOW + timedelta(seconds=2),
    )
    repeatable = classify_operation_recovery(
        operation(OperationState.DISPATCHED, IdempotencyClass.REPEATABLE),
        recovered_at=NOW + timedelta(seconds=2),
    )
    read_only = classify_operation_recovery(
        operation(OperationState.DISPATCHED, IdempotencyClass.READ_ONLY),
        recovered_at=NOW + timedelta(seconds=2),
    )

    assert prepared.action is OperationRecoveryAction.RESUME_PREPARED
    assert repeatable.action is OperationRecoveryAction.RETRY_IDEMPOTENT
    assert read_only.action is OperationRecoveryAction.RETRY_IDEMPOTENT


def test_lease_expiry_is_deadline_based_and_idempotent() -> None:
    lease = RecoveryLease(
        lease_sha256="b" * 64,
        fencing_generation=7,
        expires_at=NOW,
    )
    active = expire_recovery_lease(
        lease.model_copy(update={"expires_at": NOW + timedelta(seconds=1)}),
        recovered_at=NOW,
    )
    expired = expire_recovery_lease(lease, recovered_at=NOW)
    repeated = expire_recovery_lease(
        expired,
        recovered_at=NOW + timedelta(seconds=1),
    )

    assert active.state is LeaseRecoveryState.ACTIVE
    assert expired.state is LeaseRecoveryState.EXPIRED
    assert expired.expired_at == NOW
    assert repeated == expired
