"""Bounded child-worker workspace ownership and lifecycle management."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from app.services.harness.protocol.tasks import TaskWorkspaceMode
from app.services.harness.sandbox.worker_scope_contracts import (
    MAXIMUM_WORKER_SCOPES,
    WorkerScopeError,
    WorkerScopeRecord,
    WorkerScopeRequest,
    WorkerScopeState,
    WorkerWorkspaceOwner,
    WorkerWorkspaceView,
    compile_worker_scope,
    worker_scope_sha256,
    worker_workspace_view_sha256,
)


class InMemoryWorkerWorkspaceOwner:
    """Reference workspace owner with deterministic file-lease conflicts."""

    def __init__(self, *, maximum_active_views: int = MAXIMUM_WORKER_SCOPES) -> None:
        if not 1 <= maximum_active_views <= MAXIMUM_WORKER_SCOPES:
            raise ValueError("workspace view capacity exceeds bound")
        self._maximum_active_views = maximum_active_views
        self._views: dict[str, WorkerWorkspaceView] = {}
        self._leased_paths: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def provision(self, request: WorkerScopeRequest) -> WorkerWorkspaceView:
        async with self._lock:
            if request.scope_id in self._views:
                raise WorkerScopeError("worker workspace scope already exists")
            if len(self._views) >= self._maximum_active_views:
                raise WorkerScopeError("worker workspace capacity is exhausted")
            if request.workspace_mode is TaskWorkspaceMode.FILE_LEASE:
                self._reject_file_conflicts(request)
            view = WorkerWorkspaceView(
                scope_id=request.scope_id,
                workspace_id=request.workspace_id,
                mode=request.workspace_mode,
                allowed_paths=request.allowed_paths,
                base_view_sha256=request.base_view_sha256,
                view_sha256=worker_workspace_view_sha256(
                    scope_id=request.scope_id,
                    workspace_id=request.workspace_id,
                    mode=request.workspace_mode,
                    allowed_paths=request.allowed_paths,
                    base_view_sha256=request.base_view_sha256,
                ),
            )
            self._views[request.scope_id] = view
            if request.workspace_mode is TaskWorkspaceMode.FILE_LEASE:
                for path in request.allowed_paths:
                    self._leased_paths[(request.workspace_id, path)] = request.scope_id
            return view

    async def release(self, view: WorkerWorkspaceView) -> None:
        async with self._lock:
            existing = self._views.get(view.scope_id)
            if existing is None:
                return
            if existing.view_sha256 != view.view_sha256:
                raise WorkerScopeError("worker workspace release fence rejected")
            self._views.pop(view.scope_id)
            for key, owner_scope_id in tuple(self._leased_paths.items()):
                if owner_scope_id == view.scope_id:
                    self._leased_paths.pop(key)

    async def active_views(self) -> tuple[WorkerWorkspaceView, ...]:
        async with self._lock:
            return tuple(self._views.values())

    def _reject_file_conflicts(self, request: WorkerScopeRequest) -> None:
        for path in request.allowed_paths:
            for (workspace_id, existing_path), owner_scope_id in (
                self._leased_paths.items()
            ):
                if workspace_id != request.workspace_id:
                    continue
                if _paths_overlap(path, existing_path):
                    raise WorkerScopeError(
                        f"worker file lease conflicts with {owner_scope_id}"
                    )


class WorkerScopeManager:
    """Owns child-scope records and releases workspace views exactly once."""

    def __init__(
        self,
        workspace_owner: WorkerWorkspaceOwner,
        *,
        maximum_active_scopes: int = MAXIMUM_WORKER_SCOPES,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 1 <= maximum_active_scopes <= MAXIMUM_WORKER_SCOPES:
            raise ValueError("worker scope capacity exceeds bound")
        self._workspace_owner = workspace_owner
        self._maximum_active_scopes = maximum_active_scopes
        self._clock = clock
        self._records: dict[str, WorkerScopeRecord] = {}
        self._reservations: set[str] = set()
        self._lock = asyncio.Lock()

    async def spawn(self, request: WorkerScopeRequest) -> WorkerScopeRecord:
        async with self._lock:
            if (
                request.scope_id in self._records
                or request.scope_id in self._reservations
            ):
                raise WorkerScopeError("worker scope already exists")
            if (
                len(self._records) + len(self._reservations)
                >= self._maximum_active_scopes
            ):
                raise WorkerScopeError("worker scope capacity is exhausted")
            self._reservations.add(request.scope_id)
        try:
            view = await self._workspace_owner.provision(request)
            try:
                record = compile_worker_scope(request, view)
            except BaseException:
                await self._workspace_owner.release(view)
                raise
            async with self._lock:
                self._records[request.scope_id] = record
            return record
        finally:
            async with self._lock:
                self._reservations.discard(request.scope_id)

    async def start(self, scope_id: str) -> WorkerScopeRecord:
        return await self._transition(
            scope_id,
            allowed={WorkerScopeState.PROVISIONED},
            state=WorkerScopeState.RUNNING,
            reason=None,
            cleanup=False,
        )

    async def complete(self, scope_id: str) -> WorkerScopeRecord:
        return await self._finish(
            scope_id,
            allowed={WorkerScopeState.RUNNING},
            state=WorkerScopeState.COMPLETED,
            reason=None,
        )

    async def fail(self, scope_id: str, reason: str) -> WorkerScopeRecord:
        return await self._finish(
            scope_id,
            allowed={WorkerScopeState.PROVISIONED, WorkerScopeState.RUNNING},
            state=WorkerScopeState.FAILED,
            reason=reason,
        )

    async def cancel(self, scope_id: str, reason: str) -> WorkerScopeRecord:
        return await self._finish(
            scope_id,
            allowed={
                WorkerScopeState.PROVISIONED,
                WorkerScopeState.RUNNING,
                WorkerScopeState.CANCELLING,
            },
            state=WorkerScopeState.CANCELLED,
            reason=reason,
        )

    async def crash(self, scope_id: str, reason: str) -> WorkerScopeRecord:
        return await self._finish(
            scope_id,
            allowed={
                WorkerScopeState.PROVISIONED,
                WorkerScopeState.RUNNING,
                WorkerScopeState.CANCELLING,
            },
            state=WorkerScopeState.CRASHED,
            reason=reason,
        )

    async def get(self, scope_id: str) -> WorkerScopeRecord:
        async with self._lock:
            record = self._records.get(scope_id)
            if record is None:
                raise WorkerScopeError("worker scope does not exist")
            return record

    async def _finish(
        self,
        scope_id: str,
        *,
        allowed: set[WorkerScopeState],
        state: WorkerScopeState,
        reason: str | None,
    ) -> WorkerScopeRecord:
        record = await self._transition(
            scope_id,
            allowed=allowed,
            state=state,
            reason=reason,
            cleanup=False,
        )
        try:
            await self._workspace_owner.release(record.workspace)
        except BaseException as error:
            await self._transition(
                scope_id,
                allowed={state},
                state=WorkerScopeState.CRASHED,
                reason="workspace cleanup failed",
                cleanup=False,
            )
            raise WorkerScopeError("worker workspace cleanup failed") from error
        return await self._transition(
            scope_id,
            allowed={state},
            state=state,
            reason=reason,
            cleanup=True,
        )

    async def _transition(
        self,
        scope_id: str,
        *,
        allowed: set[WorkerScopeState],
        state: WorkerScopeState,
        reason: str | None,
        cleanup: bool,
    ) -> WorkerScopeRecord:
        async with self._lock:
            record = self._records.get(scope_id)
            if record is None:
                raise WorkerScopeError("worker scope does not exist")
            if record.state not in allowed:
                if record.state is state and record.cleanup_completed == cleanup:
                    return record
                raise WorkerScopeError("worker scope lifecycle transition rejected")
            updated_at = self._clock()
            values = record.model_dump(mode="python")
            values.update(
                {
                    "state": state,
                    "updated_at": updated_at,
                    "failure_reason": reason,
                    "cleanup_completed": cleanup,
                }
            )
            values.pop("scope_sha256", None)
            values_for_hash = record.model_dump(
                mode="json",
                exclude={"scope_sha256"},
            )
            values_for_hash.update(
                {
                    "cleanup_completed": cleanup,
                    "failure_reason": reason,
                    "state": state.value,
                    "updated_at": updated_at.isoformat().replace("+00:00", "Z"),
                }
            )
            updated = WorkerScopeRecord(
                **values,
                scope_sha256=worker_scope_sha256(values_for_hash),
            )
            self._records[scope_id] = updated
            return updated


def _paths_overlap(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )
