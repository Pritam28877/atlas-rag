"""Bounded durable task-graph node records."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ExecutionBudget,
    PrincipalId,
    ResourceUsage,
    Sha256,
    StrictProtocolModel,
    TaskGraphId,
    TaskId,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.states import TaskState


class TaskWorkspaceMode(StrEnum):
    READ_ONLY = "read_only"
    FILE_LEASE = "file_lease"
    OVERLAY = "overlay"


class TaskNodeRecord(StrictProtocolModel):
    """One bounded DAG node with explicit ownership and workspace isolation."""

    task_id: TaskId
    graph_id: TaskGraphId
    graph_sha256: Sha256
    task_spec_sha256: Sha256
    workspace_id: WorkspaceId
    parent_task_id: TaskId | None = None
    dependency_task_ids: tuple[TaskId, ...] = Field(max_length=64)
    owner_principal_id: PrincipalId
    state: TaskState
    priority: int = Field(ge=0, le=1000)
    depth: int = Field(ge=0, le=16)
    attempt: int = Field(ge=0, le=16)
    budget: ExecutionBudget
    usage: ResourceUsage
    workspace_mode: TaskWorkspaceMode
    workspace_view_sha256: Sha256
    lease_generation: int | None = Field(default=None, ge=1)
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    completed_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_graph_and_lifecycle(self) -> Self:
        dependencies = self.dependency_task_ids
        if tuple(sorted(set(dependencies))) != dependencies:
            raise ValueError("task dependencies must be unique and sorted")
        if self.task_id in dependencies:
            raise ValueError("task cannot depend on itself")
        has_parent = self.parent_task_id is not None
        if has_parent != (self.depth > 0):
            raise ValueError("task parent must agree with graph depth")
        if self.parent_task_id == self.task_id:
            raise ValueError("task cannot be its own parent")
        if self.updated_at < self.created_at:
            raise ValueError("task updated_at cannot precede created_at")
        self._validate_execution_times()
        exceeded = self.usage.exceeds(self.budget)
        if exceeded:
            raise ValueError(f"task usage exceeds budget: {', '.join(exceeded)}")
        return self

    def _validate_execution_times(self) -> None:
        requires_started_at = self.state in {
            TaskState.RUNNING,
            TaskState.CANCELLING,
            TaskState.COMPLETED,
            TaskState.FAILED,
        }
        if requires_started_at and self.started_at is None:
            raise ValueError("executed task state requires started_at")
        unstarted_states = {TaskState.PENDING, TaskState.READY}
        if self.state in unstarted_states and self.started_at is not None:
            raise ValueError("unstarted task state cannot contain started_at")
        if self.started_at is not None and self.attempt == 0:
            raise ValueError("started task requires a positive attempt")
        needs_lease = self.state in {TaskState.RUNNING, TaskState.CANCELLING}
        if needs_lease and self.lease_generation is None:
            raise ValueError("active task execution requires a fencing lease")
        if self.state in unstarted_states and self.lease_generation is not None:
            raise ValueError("unstarted task cannot contain a fencing lease")
        terminal_states = {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
        is_terminal = self.state in terminal_states
        if is_terminal != (self.completed_at is not None):
            raise ValueError("terminal task state requires completed_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("task start cannot precede creation")
        if self.completed_at is not None:
            earliest_completion = self.started_at or self.created_at
            if self.completed_at < earliest_completion:
                raise ValueError("task completion cannot precede execution")
