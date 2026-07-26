import asyncio
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

from app.services.harness.journal import (
    SQLiteEventJournal,
    SQLiteJournalIntegrityVerifier,
    SQLiteRecoveryStore,
)
from app.services.harness.journal.projection_runner import ProjectionRunner
from app.services.harness.journal.projection_store import StoredProjection
from app.services.harness.journal.sqlite_projection_store import (
    SQLiteProjectionStore,
)
from app.services.harness.protocol import (
    IdempotencyClass,
    OperationLimits,
    OperationRecord,
    OperationState,
)
from app.services.harness.protocol.recovery import (
    LeaseRecoveryState,
    RecoveryLease,
)
from app.services.harness.recovery import (
    RecoveryCoordinator,
    StartupRecoveryReport,
    StartupRecoveryStatus,
)
from scripts.probe_harness_sqlite_kill import (
    NOW,
    append_request,
    identifier,
    projection,
)

WORKSPACE_ID = identifier("wsp")


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def dispatched_non_idempotent_operation() -> OperationRecord:
    return OperationRecord(
        operation_id=identifier("opn", 1),
        turn_id=identifier("trn"),
        idempotency_key="startup-recovery-drill-operation",
        idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
        attempt=1,
        lease_fencing_token=9,
        tool_name="workspace.write_file",
        tool_version="1.0.0",
        args_sha256="a" * 64,
        capability="filesystem.write",
        policy_decision_id=identifier("dcs"),
        state=OperationState.DISPATCHED,
        limits=OperationLimits(
            max_duration_ms=1000,
            max_cpu_ms=1000,
            max_memory_bytes=16 * 1024 * 1024,
            max_output_bytes=1024,
            max_processes=1,
        ),
        prepared_at=NOW,
        dispatched_at=NOW + timedelta(seconds=1),
    )


async def run_recovery(
    path: Path,
    *,
    recovered_at_offset: int,
) -> tuple[StartupRecoveryReport, StoredProjection | None]:
    recovered_at = NOW + timedelta(seconds=recovered_at_offset)
    journal = await SQLiteEventJournal.open(path)
    projection_store = await SQLiteProjectionStore.open(
        path,
        clock=lambda: recovered_at,
    )
    recovery_store = await SQLiteRecoveryStore.open(path)
    verifier = await SQLiteJournalIntegrityVerifier.open(
        path,
        clock=lambda: recovered_at,
    )
    coordinator = RecoveryCoordinator(
        verifier,
        ProjectionRunner(journal, projection_store),
        recovery_store,
        (projection(),),
    )
    try:
        report = await coordinator.recover(
            WORKSPACE_ID,
            recovered_at=recovered_at,
        )
        stored_projection = await projection_store.load(
            WORKSPACE_ID,
            projection().name,
        )
        return report, stored_projection
    finally:
        await verifier.close()
        await recovery_store.close()
        await projection_store.close()
        await journal.close()


def test_repeated_sqlite_startup_recovery_is_fail_closed_and_idempotent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        journal = await SQLiteEventJournal.open(path, clock=lambda: NOW)
        await journal.append(append_request())
        await journal.close()

        recovery_store = await SQLiteRecoveryStore.open(path)
        await recovery_store.save_operation(
            WORKSPACE_ID,
            dispatched_non_idempotent_operation(),
            updated_at=NOW + timedelta(seconds=1),
        )
        await recovery_store.save_lease(
            WORKSPACE_ID,
            RecoveryLease(
                lease_sha256="b" * 64,
                fencing_generation=9,
                expires_at=NOW + timedelta(seconds=2),
            ),
            updated_at=NOW + timedelta(seconds=1),
        )
        await recovery_store.close()

        first_report, first_projection = await run_recovery(
            path,
            recovered_at_offset=3,
        )
        second_report, second_projection = await run_recovery(
            path,
            recovered_at_offset=4,
        )

        final_recovery_store = await SQLiteRecoveryStore.open(path)
        operations = (
            await final_recovery_store.load_recoverable_operations(WORKSPACE_ID)
        )
        active_leases = await final_recovery_store.load_active_leases(
            WORKSPACE_ID
        )
        await final_recovery_store.close()
        final_verifier = await SQLiteJournalIntegrityVerifier.open(
            path,
            clock=lambda: NOW + timedelta(seconds=5),
        )
        final_verification = await final_verifier.verify()
        await final_verifier.close()

        assert first_report.status is StartupRecoveryStatus.NEEDS_OPERATOR
        assert first_report.expired_leases == 1
        assert second_report.status is StartupRecoveryStatus.NEEDS_OPERATOR
        assert second_report.expired_leases == 0
        assert first_projection is not None
        assert second_projection is not None
        assert first_projection.checkpoint.state_json == '{"count":1}'
        assert second_projection.checkpoint == first_projection.checkpoint
        assert second_projection.generation == first_projection.generation + 1
        assert operations[0].state is OperationState.AMBIGUOUS
        assert operations[0].ambiguous_at == NOW + timedelta(seconds=3)
        assert active_leases == ()
        assert final_verification.complete

        connection = sqlite3.connect(path)
        try:
            operation_state = connection.execute(
                "SELECT operation_state FROM harness_recovery_operations"
            ).fetchone()
            lease_state = connection.execute(
                "SELECT lease_state FROM harness_recovery_leases"
            ).fetchone()
        finally:
            connection.close()
        assert operation_state == (OperationState.AMBIGUOUS.value,)
        assert lease_state == (LeaseRecoveryState.EXPIRED.value,)

    asyncio.run(scenario())
