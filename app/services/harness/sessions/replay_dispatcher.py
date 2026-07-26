"""Bounded replay-before-dispatch for authenticated mutating commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Never, Protocol

from app.services.harness.protocol import (
    CommandKind,
    CommandReplayReceipt,
    CommandResponseKind,
    IdempotencyKey,
    MutatingCommand,
    Payload,
    PrincipalId,
    WorkspaceId,
    command_request_sha256,
    command_result_sha256,
)
from app.services.harness.runtime import (
    AuthenticatedCommandContext,
    CommandDispatcher,
)

MAXIMUM_ACTIVE_REPLAY_KEYS = 64


class ReplayDispatcherErrorCode(StrEnum):
    CAPACITY = "capacity"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    RESULT_CONFLICT = "result_conflict"


class ReplayDispatcherError(RuntimeError):
    def __init__(self, code: ReplayDispatcherErrorCode) -> None:
        super().__init__("command replay operation rejected")
        self.code = code


class CommandReplayRepository(Protocol):
    async def load_command_receipt(
        self,
        workspace_id: WorkspaceId,
        principal_id: PrincipalId,
        idempotency_key: IdempotencyKey,
    ) -> CommandReplayReceipt | None: ...

    async def save_command_receipt(
        self,
        receipt: CommandReplayReceipt,
    ) -> CommandReplayReceipt: ...


@dataclass(slots=True)
class _ReplayEntry:
    lock: asyncio.Lock
    users: int


class ReplaySafeCommandDispatcher:
    """Coalesces duplicate mutation execution without owning background tasks."""

    def __init__(
        self,
        repository: CommandReplayRepository,
        inner: CommandDispatcher,
        *,
        maximum_active_replay_keys: int = 8,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 1 <= maximum_active_replay_keys <= MAXIMUM_ACTIVE_REPLAY_KEYS:
            raise ValueError("active replay keys must be between 1 and 64")
        self._repository = repository
        self._inner = inner
        self._maximum_active_replay_keys = maximum_active_replay_keys
        self._clock = clock
        self._entries: dict[tuple[WorkspaceId, IdempotencyKey], _ReplayEntry] = {}
        self._entries_lock = asyncio.Lock()

    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> Payload:
        command = context.envelope.command
        if not isinstance(command, MutatingCommand):
            return await self._inner.dispatch(context, cancellation_event)
        replay_key = (
            context.workspace.workspace_id,
            command.idempotency_key,
        )
        entry = await self._join(replay_key)
        acquired = False
        try:
            if cancellation_event.is_set():
                raise asyncio.CancelledError
            await entry.lock.acquire()
            acquired = True
            if cancellation_event.is_set():
                raise asyncio.CancelledError
            return await self._dispatch_mutation(context, cancellation_event)
        finally:
            if acquired:
                entry.lock.release()
            await self._leave(replay_key, entry)

    async def active_replay_keys(self) -> int:
        async with self._entries_lock:
            return len(self._entries)

    async def _dispatch_mutation(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> Payload:
        command = context.envelope.command
        if not isinstance(command, MutatingCommand):
            raise TypeError("mutation dispatch requires a mutating command")
        workspace_id = context.workspace.workspace_id
        principal_id = context.principal.principal_id
        request_sha256 = command_request_sha256(context.envelope)
        command_kind = CommandKind(command.kind)
        response_kind = context.contract.response
        existing = await self._repository.load_command_receipt(
            workspace_id,
            principal_id,
            command.idempotency_key,
        )
        if existing is not None:
            return self._validated_replay(
                existing,
                command_kind=command_kind,
                request_sha256=request_sha256,
                response_kind=response_kind,
            )
        result = await self._inner.dispatch(context, cancellation_event)
        committed_at = self._clock()
        self._require_utc(committed_at)
        requested_receipt = CommandReplayReceipt(
            workspace_id=workspace_id,
            principal_id=principal_id,
            idempotency_key=command.idempotency_key,
            command_kind=command_kind,
            request_sha256=request_sha256,
            response_kind=response_kind,
            result=result,
            result_sha256=command_result_sha256(result),
            committed_at=committed_at,
        )
        stored = await self._repository.save_command_receipt(requested_receipt)
        return self._validated_replay(
            stored,
            command_kind=command_kind,
            request_sha256=request_sha256,
            response_kind=response_kind,
        )

    async def _join(
        self,
        replay_key: tuple[WorkspaceId, IdempotencyKey],
    ) -> _ReplayEntry:
        async with self._entries_lock:
            entry = self._entries.get(replay_key)
            if entry is None:
                if len(self._entries) >= self._maximum_active_replay_keys:
                    self._reject(ReplayDispatcherErrorCode.CAPACITY)
                entry = _ReplayEntry(lock=asyncio.Lock(), users=0)
                self._entries[replay_key] = entry
            entry.users += 1
            return entry

    async def _leave(
        self,
        replay_key: tuple[WorkspaceId, IdempotencyKey],
        entry: _ReplayEntry,
    ) -> None:
        async with self._entries_lock:
            current = self._entries.get(replay_key)
            if current is not entry or entry.users < 1:
                raise RuntimeError("replay entry ownership is invalid")
            entry.users -= 1
            if entry.users == 0:
                del self._entries[replay_key]

    @staticmethod
    def _validated_replay(
        receipt: CommandReplayReceipt,
        *,
        command_kind: CommandKind,
        request_sha256: str,
        response_kind: CommandResponseKind,
    ) -> Payload:
        if (
            receipt.command_kind is not command_kind
            or receipt.request_sha256 != request_sha256
        ):
            ReplaySafeCommandDispatcher._reject(
                ReplayDispatcherErrorCode.IDEMPOTENCY_MISMATCH
            )
        if receipt.response_kind is not response_kind:
            ReplaySafeCommandDispatcher._reject(
                ReplayDispatcherErrorCode.RESULT_CONFLICT
            )
        return receipt.result

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("replay dispatcher clock must return UTC")

    @staticmethod
    def _reject(code: ReplayDispatcherErrorCode) -> Never:
        raise ReplayDispatcherError(code)
