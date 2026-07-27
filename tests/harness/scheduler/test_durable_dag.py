"""Fenced DAG lease and checkpoint tests."""

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from app.services.harness.scheduler import (
    DagNodeStatus,
    DagStoreConflict,
    DurableDagScheduler,
    InMemoryDurableDagStore,
    compile_task_graph,
)
from tests.harness.scheduler.test_dag_compiler import _node

GRAPH = "tgr_" + "1" * 32
WORKSPACE = "wsp_" + "2" * 32
PRINCIPAL = "prn_" + "3" * 32
OWNER = "worker-a"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_durable_scheduler_fences_checkpoints_and_unblocks_dependents() -> None:
    async def scenario() -> None:
        graph = compile_task_graph(
            graph_id=GRAPH,
            workspace_id=WORKSPACE,
            owner_principal_id=PRINCIPAL,
            nodes=(
                _node("tsk_" + "1" * 32),
                _node("tsk_" + "2" * 32, ("tsk_" + "1" * 32,)),
            ),
        ).graph
        store = InMemoryDurableDagStore(graph)
        scheduler = DurableDagScheduler(graph, store)
        first = (await scheduler.claim_ready(OWNER, now=NOW))[0]
        await scheduler.checkpoint_success(first, hashlib.sha256(b"ok").hexdigest())
        second = (await scheduler.claim_ready(OWNER, now=NOW))[0]
        assert second.task_id == "tsk_" + "2" * 32
        with pytest.raises(DagStoreConflict):
            await store.checkpoint(
                second.task_id,
                "other-worker",
                second.lease_generation,
                DagNodeStatus.COMPLETED,
                result_sha256="a" * 64,
                failure_reason=None,
            )

    asyncio.run(scenario())
