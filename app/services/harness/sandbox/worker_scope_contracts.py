"""Strict child-worker scope, budget, capability, and workspace contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Protocol, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    Capability,
    ExecutionBudget,
    PrincipalId,
    Sha256,
    StrictProtocolModel,
    TaskId,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.tasks import TaskWorkspaceMode

MAXIMUM_WORKER_DEPTH = 4
MAXIMUM_WORKER_SCOPES = 64
MAXIMUM_WORKER_PATHS = 256

type WorkspacePath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[^\x00]+$",
    ),
]


class WorkerScopeState(StrEnum):
    PROVISIONED = "provisioned"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CRASHED = "crashed"


class WorkerScopeError(RuntimeError):
    """Stable child-scope failure without exposing workspace details."""


class WorkerScopeRequest(StrictProtocolModel):
    scope_id: TaskId
    parent_scope_id: TaskId | None = None
    parent_principal_id: PrincipalId
    child_principal_id: PrincipalId
    workspace_id: WorkspaceId
    parent_capabilities: tuple[Capability, ...] = Field(max_length=64)
    requested_capabilities: tuple[Capability, ...] = Field(max_length=64)
    parent_budget: ExecutionBudget
    requested_budget: ExecutionBudget
    parent_depth: int = Field(ge=0, le=MAXIMUM_WORKER_DEPTH)
    child_depth: int = Field(ge=1, le=MAXIMUM_WORKER_DEPTH)
    workspace_mode: TaskWorkspaceMode
    allowed_paths: tuple[WorkspacePath, ...] = Field(max_length=MAXIMUM_WORKER_PATHS)
    base_view_sha256: Sha256
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_narrowing(self) -> Self:
        _require_sorted_unique(self.parent_capabilities, "parent capabilities")
        _require_sorted_unique(self.requested_capabilities, "requested capabilities")
        if not set(self.requested_capabilities).issubset(self.parent_capabilities):
            raise WorkerScopeError("child capabilities must be a parent subset")
        if self.child_depth != self.parent_depth + 1:
            raise WorkerScopeError("child depth must advance exactly one level")
        if self.child_depth > MAXIMUM_WORKER_DEPTH:
            raise WorkerScopeError("child recursion depth exceeds bound")
        _require_budget_not_broader(self.parent_budget, self.requested_budget)
        _require_canonical_paths(self.allowed_paths)
        return self


class WorkerWorkspaceView(StrictProtocolModel):
    scope_id: TaskId
    workspace_id: WorkspaceId
    mode: TaskWorkspaceMode
    allowed_paths: tuple[WorkspacePath, ...] = Field(max_length=MAXIMUM_WORKER_PATHS)
    base_view_sha256: Sha256
    view_sha256: Sha256

    @model_validator(mode="after")
    def validate_view(self) -> Self:
        _require_canonical_paths(self.allowed_paths)
        expected = worker_workspace_view_sha256(
            scope_id=self.scope_id,
            workspace_id=self.workspace_id,
            mode=self.mode,
            allowed_paths=self.allowed_paths,
            base_view_sha256=self.base_view_sha256,
        )
        if self.view_sha256 != expected:
            raise WorkerScopeError("worker workspace view hash is invalid")
        return self


class WorkerScopeRecord(StrictProtocolModel):
    scope_id: TaskId
    parent_scope_id: TaskId | None = None
    parent_principal_id: PrincipalId
    child_principal_id: PrincipalId
    workspace_id: WorkspaceId
    capabilities: tuple[Capability, ...] = Field(max_length=64)
    budget: ExecutionBudget
    depth: int = Field(ge=1, le=MAXIMUM_WORKER_DEPTH)
    workspace: WorkerWorkspaceView
    state: WorkerScopeState
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    cleanup_completed: bool = False
    failure_reason: str | None = Field(default=None, max_length=2_048)
    scope_sha256: Sha256

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        _require_sorted_unique(self.capabilities, "worker capabilities")
        if self.scope_id != self.workspace.scope_id:
            raise WorkerScopeError("worker scope and workspace IDs disagree")
        if self.workspace_id != self.workspace.workspace_id:
            raise WorkerScopeError("worker workspace IDs disagree")
        if self.updated_at < self.created_at:
            raise WorkerScopeError("worker update precedes creation")
        terminal = self.state in {
            WorkerScopeState.COMPLETED,
            WorkerScopeState.FAILED,
            WorkerScopeState.CANCELLED,
            WorkerScopeState.CRASHED,
        }
        if self.cleanup_completed and not terminal:
            raise WorkerScopeError("active worker cannot report completed cleanup")
        if self.state in {
            WorkerScopeState.FAILED,
            WorkerScopeState.CANCELLED,
            WorkerScopeState.CRASHED,
        } and not self.failure_reason:
            raise WorkerScopeError("failed worker state requires a reason")
        expected = worker_scope_sha256(self.model_dump(mode="json"))
        if self.scope_sha256 != expected:
            raise WorkerScopeError("worker scope hash is invalid")
        return self


class WorkerWorkspaceOwner(Protocol):
    async def provision(self, request: WorkerScopeRequest) -> WorkerWorkspaceView: ...

    async def release(self, view: WorkerWorkspaceView) -> None: ...


def compile_worker_scope(
    request: WorkerScopeRequest,
    workspace: WorkerWorkspaceView,
) -> WorkerScopeRecord:
    if (
        workspace.scope_id != request.scope_id
        or workspace.workspace_id != request.workspace_id
        or workspace.mode is not request.workspace_mode
        or workspace.allowed_paths != request.allowed_paths
        or workspace.base_view_sha256 != request.base_view_sha256
    ):
        raise WorkerScopeError("provisioned workspace exceeds requested scope")
    capabilities = tuple(request.requested_capabilities)
    values = {
        "scope_id": request.scope_id,
        "parent_scope_id": request.parent_scope_id,
        "parent_principal_id": request.parent_principal_id,
        "child_principal_id": request.child_principal_id,
        "workspace_id": request.workspace_id,
        "capabilities": capabilities,
        "budget": request.requested_budget,
        "depth": request.child_depth,
        "workspace": workspace,
        "state": WorkerScopeState.PROVISIONED,
        "created_at": request.created_at,
        "updated_at": request.created_at,
        "cleanup_completed": False,
        "failure_reason": None,
    }
    provisional = WorkerScopeRecord.model_construct(
        scope_id=request.scope_id,
        parent_scope_id=request.parent_scope_id,
        parent_principal_id=request.parent_principal_id,
        child_principal_id=request.child_principal_id,
        workspace_id=request.workspace_id,
        capabilities=capabilities,
        budget=request.requested_budget,
        depth=request.child_depth,
        workspace=workspace,
        state=WorkerScopeState.PROVISIONED,
        created_at=request.created_at,
        updated_at=request.created_at,
        cleanup_completed=False,
        failure_reason=None,
    )
    return WorkerScopeRecord.model_validate(
        {
            **values,
            "scope_sha256": worker_scope_sha256(
                provisional.model_dump(mode="json", warnings=False)
            ),
        }
    )


def worker_workspace_view_sha256(
    *,
    scope_id: TaskId,
    workspace_id: WorkspaceId,
    mode: TaskWorkspaceMode,
    allowed_paths: Sequence[WorkspacePath],
    base_view_sha256: Sha256,
) -> str:
    return _canonical_sha256(
        {
            "allowed_paths": list(allowed_paths),
            "base_view_sha256": base_view_sha256,
            "mode": mode.value,
            "scope_id": scope_id,
            "workspace_id": workspace_id,
        }
    )


def worker_scope_sha256(values: object) -> str:
    if isinstance(values, dict):
        values = {key: value for key, value in values.items() if key != "scope_sha256"}
    return _canonical_sha256(values)


def _require_sorted_unique(values: Sequence[str], field_name: str) -> None:
    if tuple(sorted(set(values))) != tuple(values):
        raise WorkerScopeError(f"{field_name} must be unique and sorted")


def _require_canonical_paths(paths: Sequence[WorkspacePath]) -> None:
    for path_value in paths:
        path = PurePosixPath(path_value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != path_value
        ):
            raise WorkerScopeError("worker path must be relative and traversal-free")
    _require_sorted_unique(paths, "worker paths")


def _require_budget_not_broader(
    parent: ExecutionBudget,
    requested: ExecutionBudget,
) -> None:
    parent_values = parent.model_dump(mode="python")
    requested_values = requested.model_dump(mode="python")
    for field_name, requested_value in requested_values.items():
        if requested_value > parent_values[field_name]:
            raise WorkerScopeError("child budget cannot exceed parent budget")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=lambda item: item.value if isinstance(item, StrEnum) else item,
        ).encode("utf-8")
    ).hexdigest()
