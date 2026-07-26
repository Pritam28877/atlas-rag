import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.harness.journal import JournalVerificationResult
from app.services.harness.journal.projection_contracts import ProjectionDefinition
from app.services.harness.protocol import (
    IdempotencyClass,
    OperationLimits,
    OperationRecord,
    OperationState,
    StrictProtocolModel,
)
from app.services.harness.protocol.recovery import (
    LeaseRecoveryState,
    RecoveryLease,
)
from app.services.harness.recovery import (
    RecoveryCoordinator,
    RecoveryCoordinatorError,
    RecoveryCoordinatorErrorCode,
    StartupRecoveryStatus,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "3" * 32


class CounterState(StrictProtocolModel):
    count: int


def identifier(prefix: str, number: int) -> str:
    return f"{prefix}_{number:032x}"


def operation(
    number: int,
    state: OperationState,
    idempotency_class: IdempotencyClass,
) -> OperationRecord:
    values: dict[str, object] = {
        "operation_id": identifier("opn", number),
        "turn_id": identifier("trn", number),
        "idempotency_key": f"recovery-coordinator-{number:04d}",
        "idempotency_class": idempotency_class,
        "attempt": 1,
        "lease_fencing_token": number,
        "tool_name": "workspace.write_file",
        "tool_version": "1.0.0",
        "args_sha256": f"{number:064x}",
        "capability": "filesystem.write",
        "policy_decision_id": identifier("dcs", number),
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


def projection(name: str) -> ProjectionDefinition[CounterState]:
    def reducer(
        state: CounterState,
        event: Any,
    ) -> CounterState:
        _ = event
        return CounterState(count=state.count + 1)

    return ProjectionDefinition(
        name=name,
        version="1.0",
        state_model=CounterState,
        initial_state=CounterState(count=0),
        reducer=reducer,
    )


class FakeIntegrityVerifier:
    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete
        self.calls = 0

    async def verify(
        self,
        *,
        maximum_records: int,
    ) -> JournalVerificationResult:
        self.calls += 1
        return JournalVerificationResult(
            complete=self.complete,
            events_checked=min(maximum_records, 4),
            receipts_checked=0,
            projections_checked=0,
            verified_at=NOW if self.complete else None,
        )


class FakeProjectionRebuilder:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def rebuild(
        self,
        definition: ProjectionDefinition[Any],
        workspace_id: str,
        *,
        maximum_pages: int,
    ) -> object:
        assert workspace_id == WORKSPACE_ID
        assert maximum_pages == 7
        self.names.append(definition.name)
        return object()


class FakeRecoveryStore:
    def __init__(
        self,
        operations: tuple[OperationRecord, ...],
        leases: tuple[RecoveryLease, ...],
    ) -> None:
        self.operations = {
            item.operation_id: item
            for item in operations
        }
        self.leases = {
            item.lease_sha256: item
            for item in leases
        }
        self.operation_saves = 0
        self.lease_saves = 0
        self.load_calls = 0

    async def load_recoverable_operations(
        self,
        workspace_id: str,
        *,
        maximum_records: int,
    ) -> tuple[OperationRecord, ...]:
        assert workspace_id == WORKSPACE_ID
        assert maximum_records == 10
        self.load_calls += 1
        return tuple(self.operations.values())

    async def save_operation(
        self,
        workspace_id: str,
        operation_record: OperationRecord,
        *,
        updated_at: datetime,
    ) -> OperationRecord:
        assert workspace_id == WORKSPACE_ID
        assert updated_at == NOW + timedelta(seconds=2)
        self.operations[operation_record.operation_id] = operation_record
        self.operation_saves += 1
        return operation_record

    async def load_active_leases(
        self,
        workspace_id: str,
        *,
        maximum_records: int,
    ) -> tuple[RecoveryLease, ...]:
        assert workspace_id == WORKSPACE_ID
        assert maximum_records == 10
        self.load_calls += 1
        return tuple(
            lease
            for lease in self.leases.values()
            if lease.state is LeaseRecoveryState.ACTIVE
        )

    async def save_lease(
        self,
        workspace_id: str,
        lease: RecoveryLease,
        *,
        updated_at: datetime,
    ) -> RecoveryLease:
        assert workspace_id == WORKSPACE_ID
        assert updated_at == NOW + timedelta(seconds=2)
        self.leases[lease.lease_sha256] = lease
        self.lease_saves += 1
        return lease


def test_recovery_is_integrity_first_ordered_and_idempotent() -> None:
    async def scenario() -> None:
        verifier = FakeIntegrityVerifier()
        rebuilder = FakeProjectionRebuilder()
        store = FakeRecoveryStore(
            operations=(
                operation(
                    1,
                    OperationState.PREPARED,
                    IdempotencyClass.NON_IDEMPOTENT,
                ),
                operation(
                    2,
                    OperationState.DISPATCHED,
                    IdempotencyClass.REPEATABLE,
                ),
                operation(
                    3,
                    OperationState.DISPATCHED,
                    IdempotencyClass.NON_IDEMPOTENT,
                ),
            ),
            leases=(
                RecoveryLease(
                    lease_sha256="a" * 64,
                    fencing_generation=1,
                    expires_at=NOW + timedelta(seconds=1),
                ),
                RecoveryLease(
                    lease_sha256="b" * 64,
                    fencing_generation=2,
                    expires_at=NOW + timedelta(seconds=10),
                ),
            ),
        )
        coordinator = RecoveryCoordinator(
            verifier,
            rebuilder,
            store,
            (projection("zeta"), projection("alpha")),
        )

        report = await coordinator.recover(
            WORKSPACE_ID,
            recovered_at=NOW + timedelta(seconds=2),
            maximum_projection_pages=7,
            maximum_recovery_records=10,
        )
        repeated = await coordinator.recover(
            WORKSPACE_ID,
            recovered_at=NOW + timedelta(seconds=2),
            maximum_projection_pages=7,
            maximum_recovery_records=10,
        )

        assert report.status is StartupRecoveryStatus.NEEDS_OPERATOR
        assert report.projections_rebuilt == 2
        assert report.operations_checked == 3
        assert report.prepared_operations == 1
        assert report.retryable_operations == 1
        assert report.needs_operator_operations == 1
        assert report.active_leases == 1
        assert report.expired_leases == 1
        assert repeated.expired_leases == 0
        assert rebuilder.names == ["alpha", "zeta", "alpha", "zeta"]
        assert store.operation_saves == 1
        assert store.lease_saves == 1
        assert store.operations[identifier("opn", 3)].state is (
            OperationState.AMBIGUOUS
        )

    asyncio.run(scenario())


def test_incomplete_integrity_stops_before_rebuild_or_reconciliation() -> None:
    async def scenario() -> None:
        verifier = FakeIntegrityVerifier(complete=False)
        rebuilder = FakeProjectionRebuilder()
        store = FakeRecoveryStore((), ())
        coordinator = RecoveryCoordinator(
            verifier,
            rebuilder,
            store,
            (projection("counter"),),
        )

        with pytest.raises(RecoveryCoordinatorError) as captured:
            await coordinator.recover(
                WORKSPACE_ID,
                recovered_at=NOW,
                maximum_projection_pages=7,
                maximum_recovery_records=10,
            )

        assert captured.value.code is RecoveryCoordinatorErrorCode.INTEGRITY_LIMIT
        assert rebuilder.names == []
        assert store.load_calls == 0

    asyncio.run(scenario())
