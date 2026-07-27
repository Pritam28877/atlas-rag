"""Child-worker capability, budget, workspace, and cleanup tests."""

import asyncio
from datetime import UTC, datetime

import pytest

from app.services.harness.protocol import ExecutionBudget
from app.services.harness.protocol.tasks import TaskWorkspaceMode
from app.services.harness.sandbox import (
    InMemoryWorkerWorkspaceOwner,
    WorkerScopeError,
    WorkerScopeManager,
    WorkerScopeRequest,
    WorkerScopeState,
    WorkerWorkspaceView,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
WORKSPACE = "wsp_" + "2" * 32
PARENT = "prn_" + "3" * 32
CHILD = "prn_" + "4" * 32
BASE_VIEW = "a" * 64


def _budget(limit: int = 10) -> ExecutionBudget:
    return ExecutionBudget(
        max_steps=limit,
        max_tool_calls=limit,
        max_input_tokens=limit,
        max_output_tokens=limit,
        max_tool_output_bytes=limit,
        max_duration_ms=limit * 100,
        max_cost_microusd=limit,
    )


def _request(
    suffix: str,
    *,
    capabilities: tuple[str, ...] = ("filesystem.read",),
    budget: ExecutionBudget | None = None,
    paths: tuple[str, ...] = ("src",),
    mode: TaskWorkspaceMode = TaskWorkspaceMode.FILE_LEASE,
) -> WorkerScopeRequest:
    return WorkerScopeRequest(
        scope_id="tsk_" + suffix * 32,
        parent_principal_id=PARENT,
        child_principal_id=CHILD,
        workspace_id=WORKSPACE,
        parent_capabilities=("filesystem.read", "filesystem.write"),
        requested_capabilities=capabilities,
        parent_budget=_budget(10),
        requested_budget=budget or _budget(5),
        parent_depth=0,
        child_depth=1,
        workspace_mode=mode,
        allowed_paths=paths,
        base_view_sha256=BASE_VIEW,
        created_at=NOW,
    )


def test_child_scope_rejects_capability_budget_and_depth_expansion() -> None:
    with pytest.raises(WorkerScopeError, match="capabilities"):
        _request("1", capabilities=("network.connect",))
    with pytest.raises(WorkerScopeError, match="budget"):
        _request("2", budget=_budget(11))
    values = _request("3").model_dump(mode="python")
    values["child_depth"] = 2
    with pytest.raises(WorkerScopeError, match="child depth"):
        WorkerScopeRequest.model_validate(values)


def test_file_leases_conflict_and_cleanup_is_idempotent() -> None:
    async def scenario() -> None:
        owner = InMemoryWorkerWorkspaceOwner()
        manager = WorkerScopeManager(owner, clock=lambda: NOW)
        first = await manager.spawn(_request("1"))
        with pytest.raises(WorkerScopeError, match="conflicts"):
            await manager.spawn(_request("2", paths=("src/main.py",)))
        await manager.start(first.scope_id)
        cancelled = await manager.cancel(first.scope_id, "caller cancelled")
        repeated = await manager.get(first.scope_id)

        assert cancelled.state is WorkerScopeState.CANCELLED
        assert cancelled.cleanup_completed
        assert repeated == cancelled
        assert await owner.active_views() == ()

    asyncio.run(scenario())


def test_overlay_scopes_can_run_concurrently_and_crash_is_retained() -> None:
    async def scenario() -> None:
        owner = InMemoryWorkerWorkspaceOwner(maximum_active_views=2)
        manager = WorkerScopeManager(owner, maximum_active_scopes=2, clock=lambda: NOW)
        requests = (
            _request("1", mode=TaskWorkspaceMode.OVERLAY),
            _request("2", mode=TaskWorkspaceMode.OVERLAY),
        )
        records = await asyncio.gather(
            *(manager.spawn(request) for request in requests)
        )
        await asyncio.gather(*(manager.start(record.scope_id) for record in records))
        crashed = await manager.crash(records[0].scope_id, "worker process exited")
        completed = await manager.complete(records[1].scope_id)

        assert crashed.state is WorkerScopeState.CRASHED
        assert crashed.cleanup_completed
        assert completed.state is WorkerScopeState.COMPLETED
        assert completed.cleanup_completed
        assert await owner.active_views() == ()

    asyncio.run(scenario())


def test_cleanup_failure_is_retained_as_crashed_scope() -> None:
    class FailingOwner(InMemoryWorkerWorkspaceOwner):
        async def release(self, view: WorkerWorkspaceView) -> None:
            raise RuntimeError("simulated cleanup failure")

    async def scenario() -> None:
        owner = FailingOwner()
        manager = WorkerScopeManager(owner, clock=lambda: NOW)
        record = await manager.spawn(_request("4"))
        await manager.start(record.scope_id)
        with pytest.raises(WorkerScopeError, match="cleanup"):
            await manager.complete(record.scope_id)
        retained = await manager.get(record.scope_id)

        assert retained.state is WorkerScopeState.CRASHED
        assert not retained.cleanup_completed
        assert len(await owner.active_views()) == 1

    asyncio.run(scenario())
