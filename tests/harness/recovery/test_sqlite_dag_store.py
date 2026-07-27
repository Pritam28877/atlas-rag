"""SQLite persistence and fencing tests for durable DAG scheduling."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.recovery import SQLiteDurableDagStore
from app.services.harness.scheduler import (
    DagNodeSpec,
    DagNodeStatus,
    DagStoreConflict,
    DurableDagScheduler,
    TaskGraphDefinition,
    compile_task_graph,
)

GRAPH_ID = "tgr_" + "1" * 32
WORKSPACE_ID = "wsp_" + "2" * 32
PRINCIPAL_ID = "prn_" + "3" * 32
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _graph(second_priority: int = 1) -> TaskGraphDefinition:
    first_task = "tsk_" + "1" * 32
    second_task = "tsk_" + "2" * 32
    return compile_task_graph(
        graph_id=GRAPH_ID,
        workspace_id=WORKSPACE_ID,
        owner_principal_id=PRINCIPAL_ID,
        nodes=(
            DagNodeSpec(
                task_id=first_task,
                dependency_task_ids=(),
                role="worker",
                depth=0,
                priority=1,
                task_spec_sha256=hashlib.sha256(first_task.encode()).hexdigest(),
            ),
            DagNodeSpec(
                task_id=second_task,
                dependency_task_ids=(first_task,),
                role="worker",
                depth=1,
                priority=second_priority,
                task_spec_sha256=hashlib.sha256(second_task.encode()).hexdigest(),
            ),
        ),
    ).graph


def _owner(path: Path) -> SQLiteConnectionOwner:
    return SQLiteConnectionOwner(
        path,
        busy_timeout_ms=5_000,
        maximum_pending_operations=4,
    )


def test_sqlite_dag_store_survives_restart_and_rejects_stale_lease(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "dag.sqlite3"
        first_owner = _owner(path)
        await first_owner.initialize()
        first_store = SQLiteDurableDagStore(first_owner, _graph())
        await first_store.initialize()
        first_scheduler = DurableDagScheduler(_graph(), first_store)
        first_lease = (await first_scheduler.claim_ready(
            "worker-a",
            now=NOW,
            lease_duration_ms=100,
        ))[0]
        await first_owner.close()

        restarted_owner = _owner(path)
        await restarted_owner.initialize()
        restarted_store = SQLiteDurableDagStore(restarted_owner, _graph())
        await restarted_store.initialize()
        restarted_scheduler = DurableDagScheduler(_graph(), restarted_store)
        second_lease = (await restarted_scheduler.claim_ready(
            "worker-b",
            now=NOW + timedelta(milliseconds=200),
            lease_duration_ms=100,
        ))[0]
        assert second_lease.lease_generation == 2
        with pytest.raises(DagStoreConflict):
            await restarted_store.checkpoint(
                first_lease.task_id,
                first_lease.owner_id,
                first_lease.lease_generation,
                status=DagNodeStatus.COMPLETED,
                result_sha256="a" * 64,
                failure_reason=None,
            )
        await restarted_scheduler.checkpoint_success(
            second_lease,
            hashlib.sha256(b"result").hexdigest(),
        )
        records = await restarted_store.records()
        await restarted_owner.close()

        assert records[0].status.value == "completed"
        assert records[0].lease_generation == 2

    asyncio.run(scenario())


def test_sqlite_dag_store_allows_only_one_concurrent_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "dag.sqlite3"
        first_owner = _owner(path)
        second_owner = _owner(path)
        await first_owner.initialize()
        first_store = SQLiteDurableDagStore(first_owner, _graph())
        await first_store.initialize()
        await second_owner.initialize()
        second_store = SQLiteDurableDagStore(second_owner, _graph())
        await second_store.initialize()
        first_scheduler = DurableDagScheduler(_graph(), first_store)
        second_scheduler = DurableDagScheduler(_graph(), second_store)
        claims = await asyncio.gather(
            first_scheduler.claim_ready("worker-a", now=NOW),
            second_scheduler.claim_ready("worker-b", now=NOW),
        )
        await first_owner.close()
        await second_owner.close()

        assert sum(len(leases) for leases in claims) == 1

    asyncio.run(scenario())


def test_sqlite_dag_store_rejects_graph_hash_reuse(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "dag.sqlite3"
        owner = _owner(path)
        await owner.initialize()
        store = SQLiteDurableDagStore(owner, _graph())
        await store.initialize()
        with pytest.raises(DagStoreConflict):
            await SQLiteDurableDagStore(owner, _graph(second_priority=2)).initialize()
        await owner.close()

    asyncio.run(scenario())
