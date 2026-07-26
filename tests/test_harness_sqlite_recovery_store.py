import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import (
    RecoveryStoreConflict,
    SQLiteRecoveryStore,
)
from app.services.harness.protocol import (
    IdempotencyClass,
    OperationLimits,
    OperationRecord,
    OperationState,
)
from app.services.harness.runtime import (
    LeaseRecoveryState,
    RecoveryLease,
    classify_operation_recovery,
    expire_recovery_lease,
)

NOW = datetime(2026, 7, 27, 11, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32
OTHER_WORKSPACE_ID = "wsp_" + "2" * 32


def identifier(prefix: str, number: int) -> str:
    return f"{prefix}_{number:032x}"


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def operation(
    number: int,
    *,
    state: OperationState = OperationState.DISPATCHED,
    idempotency_class: IdempotencyClass = IdempotencyClass.NON_IDEMPOTENT,
    attempt: int = 1,
) -> OperationRecord:
    values: dict[str, object] = {
        "operation_id": identifier("opn", number),
        "turn_id": identifier("trn", number),
        "idempotency_key": f"recovery-operation-{number:04d}",
        "idempotency_class": idempotency_class,
        "attempt": attempt,
        "lease_fencing_token": number + 1,
        "tool_name": "workspace.write_file",
        "tool_version": "1.0.0",
        "args_sha256": f"{number + 1:064x}",
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


def test_restart_recovers_and_classifies_ambiguous_operation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        store = await SQLiteRecoveryStore.open(path)
        dispatched = operation(1)
        await store.save_operation(
            WORKSPACE_ID,
            dispatched,
            updated_at=NOW + timedelta(seconds=1),
        )
        await store.close()

        restarted = await SQLiteRecoveryStore.open(path)
        recovered = await restarted.load_recoverable_operations(WORKSPACE_ID)
        decision = classify_operation_recovery(
            recovered[0],
            recovered_at=NOW + timedelta(seconds=2),
        )
        first = await restarted.save_operation(
            WORKSPACE_ID,
            decision.operation,
            updated_at=NOW + timedelta(seconds=2),
        )
        repeated = await restarted.save_operation(
            WORKSPACE_ID,
            decision.operation,
            updated_at=NOW + timedelta(seconds=3),
        )
        await restarted.close()

        verified = await SQLiteRecoveryStore.open(path)
        durable = await verified.load_recoverable_operations(WORKSPACE_ID)
        await verified.close()

        assert first == decision.operation
        assert repeated == decision.operation
        assert durable == (decision.operation,)
        assert durable[0].state is OperationState.AMBIGUOUS

    asyncio.run(scenario())


def test_restart_expires_lease_once_and_removes_it_from_active_set(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        lease = RecoveryLease(
            lease_sha256="a" * 64,
            fencing_generation=7,
            expires_at=NOW + timedelta(seconds=1),
        )
        store = await SQLiteRecoveryStore.open(path)
        await store.save_lease(WORKSPACE_ID, lease, updated_at=NOW)
        await store.close()

        restarted = await SQLiteRecoveryStore.open(path)
        active = await restarted.load_active_leases(WORKSPACE_ID)
        expired = expire_recovery_lease(
            active[0],
            recovered_at=NOW + timedelta(seconds=2),
        )
        first = await restarted.save_lease(
            WORKSPACE_ID,
            expired,
            updated_at=NOW + timedelta(seconds=2),
        )
        repeated = await restarted.save_lease(
            WORKSPACE_ID,
            expired,
            updated_at=NOW + timedelta(seconds=3),
        )
        remaining = await restarted.load_active_leases(WORKSPACE_ID)
        await restarted.close()

        assert first.state is LeaseRecoveryState.EXPIRED
        assert repeated == first
        assert remaining == ()

    asyncio.run(scenario())


def test_recovery_store_is_workspace_scoped_and_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await SQLiteRecoveryStore.open(database_path(tmp_path))
        first = operation(1, state=OperationState.PREPARED)
        second = operation(2, state=OperationState.PREPARED)
        await store.save_operation(WORKSPACE_ID, first, updated_at=NOW)
        await store.save_operation(WORKSPACE_ID, second, updated_at=NOW)
        await store.save_operation(OTHER_WORKSPACE_ID, first, updated_at=NOW)

        isolated = await store.load_recoverable_operations(OTHER_WORKSPACE_ID)
        with pytest.raises(RecoveryStoreConflict, match="limit exceeded"):
            await store.load_recoverable_operations(
                WORKSPACE_ID,
                maximum_records=1,
            )
        with pytest.raises(ValueError, match="between 1 and 10000"):
            await store.load_recoverable_operations(
                WORKSPACE_ID,
                maximum_records=0,
            )
        await store.close()

        assert isolated == (first,)

    asyncio.run(scenario())


def test_recovery_store_rejects_conflicting_or_invalid_transitions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = await SQLiteRecoveryStore.open(database_path(tmp_path))
        dispatched = operation(1)
        await store.save_operation(
            WORKSPACE_ID,
            dispatched,
            updated_at=NOW + timedelta(seconds=1),
        )

        conflicting = operation(1, attempt=2)
        with pytest.raises(RecoveryStoreConflict, match="different evidence"):
            await store.save_operation(
                WORKSPACE_ID,
                conflicting,
                updated_at=NOW + timedelta(seconds=2),
            )

        prepared = operation(2, state=OperationState.PREPARED)
        completed = OperationRecord.model_validate(
            {
                **operation(2).model_dump(),
                "state": OperationState.COMPLETED,
                "terminal_at": NOW + timedelta(seconds=2),
                "result_sha256": "f" * 64,
            }
        )
        await store.save_operation(WORKSPACE_ID, prepared, updated_at=NOW)
        with pytest.raises(RecoveryStoreConflict, match="durable"):
            await store.save_operation(
                WORKSPACE_ID,
                completed,
                updated_at=NOW + timedelta(seconds=2),
            )
        await store.close()

    asyncio.run(scenario())
