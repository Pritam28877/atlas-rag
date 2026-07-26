from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ExecutionBudget,
    ResourceUsage,
    TaskNodeRecord,
    TaskState,
    TaskWorkspaceMode,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


def budget() -> ExecutionBudget:
    return ExecutionBudget(
        max_steps=8,
        max_tool_calls=4,
        max_input_tokens=16_000,
        max_output_tokens=4_000,
        max_tool_output_bytes=1024,
        max_duration_ms=30_000,
        max_cost_microusd=250_000,
    )


def usage(**overrides: int) -> ResourceUsage:
    values = {
        "steps": 1,
        "tool_calls": 0,
        "input_tokens": 100,
        "output_tokens": 20,
        "tool_output_bytes": 0,
        "duration_ms": 1_000,
        "cost_microusd": 1_000,
    }
    values.update(overrides)
    return ResourceUsage(**values)


def task(**overrides: object) -> TaskNodeRecord:
    values: dict[str, object] = {
        "task_id": identifier("tsk"),
        "graph_id": identifier("tgr"),
        "graph_sha256": "1" * 64,
        "task_spec_sha256": "2" * 64,
        "workspace_id": identifier("wsp"),
        "dependency_task_ids": (
            identifier("tsk", "1"),
            identifier("tsk", "2"),
        ),
        "owner_principal_id": identifier("prn"),
        "state": TaskState.RUNNING,
        "priority": 100,
        "depth": 0,
        "attempt": 1,
        "budget": budget(),
        "usage": usage(),
        "workspace_mode": TaskWorkspaceMode.OVERLAY,
        "workspace_view_sha256": "0" * 64,
        "lease_generation": 7,
        "created_at": NOW,
        "updated_at": NOW + timedelta(seconds=1),
        "started_at": NOW + timedelta(seconds=1),
    }
    values.update(overrides)
    return TaskNodeRecord.model_validate(values)


def test_task_graph_edges_are_canonical_and_non_recursive() -> None:
    assert task().dependency_task_ids[0] == identifier("tsk", "1")

    with pytest.raises(ValidationError, match="unique and sorted"):
        task(
            dependency_task_ids=(
                identifier("tsk", "2"),
                identifier("tsk", "1"),
            )
        )
    with pytest.raises(ValidationError, match="depend on itself"):
        task(dependency_task_ids=(identifier("tsk"),))
    with pytest.raises(ValidationError, match="parent must agree"):
        task(parent_task_id=identifier("tsk", "3"))
    with pytest.raises(ValidationError, match="own parent"):
        task(parent_task_id=identifier("tsk"), depth=1)


def test_active_task_requires_start_attempt_and_fencing_lease() -> None:
    assert task().lease_generation == 7

    with pytest.raises(ValidationError, match="requires started_at"):
        task(started_at=None)
    with pytest.raises(ValidationError, match="positive attempt"):
        task(attempt=0)
    with pytest.raises(ValidationError, match="requires a fencing lease"):
        task(lease_generation=None)
    with pytest.raises(ValidationError, match="unstarted task"):
        task(
            state=TaskState.READY,
            attempt=0,
            started_at=None,
        )


def test_task_terminal_time_and_resource_budget_are_enforced() -> None:
    completed_at = NOW + timedelta(seconds=10)
    completed = task(
        state=TaskState.COMPLETED,
        completed_at=completed_at,
    )
    assert completed.completed_at == completed_at

    with pytest.raises(ValidationError, match="requires completed_at"):
        task(state=TaskState.FAILED)
    with pytest.raises(ValidationError, match="completion cannot precede execution"):
        task(
            state=TaskState.COMPLETED,
            completed_at=NOW,
        )
    with pytest.raises(ValidationError, match="input_tokens"):
        task(usage=usage(input_tokens=16_001))


def test_cancelled_pending_task_does_not_require_execution_start() -> None:
    cancelled = task(
        state=TaskState.CANCELLED,
        attempt=0,
        usage=usage(
            steps=0,
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
            cost_microusd=0,
        ),
        lease_generation=None,
        started_at=None,
        completed_at=NOW + timedelta(seconds=1),
    )

    assert cancelled.started_at is None
