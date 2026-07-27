"""Real SQLite durability proof for prepared and dispatched operations."""

import asyncio
import os
from datetime import timedelta
from pathlib import Path

from app.services.harness.journal import SQLiteRecoveryStore
from app.services.harness.protocol import OperationState
from app.services.harness.tools.operation_lifecycle import DurableOperationLifecycle
from tests.harness.operations.fixtures import (
    NOW,
    WORKSPACE_ID,
    fence,
    request,
)


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def test_restart_observes_each_admission_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        store = await SQLiteRecoveryStore.open(path)
        lifecycle = DurableOperationLifecycle(store)
        prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)
        await store.close()

        after_prepare = await SQLiteRecoveryStore.open(path)
        prepared_records = await after_prepare.load_recoverable_operations(
            WORKSPACE_ID
        )
        prepared_receipts = await after_prepare.load_admission_receipts(
            WORKSPACE_ID,
            prepared.operation.operation_id,
        )
        assert prepared_records == (prepared.operation,)
        assert prepared_receipts == (prepared.receipt,)

        lifecycle = DurableOperationLifecycle(after_prepare)
        permit = await lifecycle.dispatch(
            prepared,
            fence(),
            dispatched_at=NOW + timedelta(seconds=1),
        )
        await after_prepare.close()

        after_dispatch = await SQLiteRecoveryStore.open(path)
        dispatched_records = await after_dispatch.load_recoverable_operations(
            WORKSPACE_ID
        )
        dispatched_receipts = await after_dispatch.load_admission_receipts(
            WORKSPACE_ID,
            permit.operation.operation_id,
        )
        await after_dispatch.close()
        assert dispatched_records == (permit.operation,)
        assert dispatched_receipts == (
            permit.prepared_receipt,
            permit.dispatched_receipt,
        )
        assert dispatched_records[0].state is OperationState.DISPATCHED

    asyncio.run(scenario())
