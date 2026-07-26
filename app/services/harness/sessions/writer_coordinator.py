"""Bounded single-writer leases and coalesced wakes per session."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Never

from pydantic import Field

from app.services.harness.protocol import (
    Sha256,
    StrictProtocolModel,
    ThreadId,
    UtcTimestamp,
)

MAXIMUM_ACTIVE_SESSIONS = 64


class WriterCoordinatorErrorCode(StrEnum):
    CAPACITY = "capacity"
    BUSY = "busy"
    FENCED = "fenced"
    NO_SESSION = "no_session"


class WriterCoordinatorError(RuntimeError):
    def __init__(self, code: WriterCoordinatorErrorCode) -> None:
        super().__init__("session writer operation rejected")
        self.code = code


class WakeDisposition(StrEnum):
    ENQUEUED = "enqueued"
    COALESCED = "coalesced"


class SessionWriterLease(StrictProtocolModel):
    thread_id: ThreadId
    owner_sha256: Sha256
    fencing_generation: int = Field(ge=1, le=2**63 - 1)
    acquired_at: UtcTimestamp
    expires_at: UtcTimestamp


@dataclass(slots=True)
class _SessionEntry:
    lease: SessionWriterLease | None
    wake_pending: bool


class SessionWriterCoordinator:
    def __init__(
        self,
        *,
        maximum_active_sessions: int = 8,
        lease_seconds: int = 30,
    ) -> None:
        if not 1 <= maximum_active_sessions <= MAXIMUM_ACTIVE_SESSIONS:
            raise ValueError("active sessions must be between 1 and 64")
        if not 1 <= lease_seconds <= 3_600:
            raise ValueError("writer lease must be between 1 and 3600 seconds")
        self._maximum_active_sessions = maximum_active_sessions
        self._lease_duration = timedelta(seconds=lease_seconds)
        self._entries: dict[ThreadId, _SessionEntry] = {}
        self._next_generation = 1
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        thread_id: ThreadId,
        owner_sha256: Sha256,
        *,
        acquired_at: datetime,
    ) -> SessionWriterLease:
        self._require_utc(acquired_at)
        async with self._lock:
            self._prune_expired(acquired_at)
            entry = self._entries.get(thread_id)
            if entry is not None and entry.lease is not None:
                current = entry.lease
                if current.expires_at > acquired_at:
                    if current.owner_sha256 == owner_sha256:
                        return current
                    self._reject(WriterCoordinatorErrorCode.BUSY)
                entry.lease = None
            if (
                entry is None
                and len(self._entries) >= self._maximum_active_sessions
            ):
                self._reject(WriterCoordinatorErrorCode.CAPACITY)
            generation = self._claim_generation()
            lease = SessionWriterLease(
                thread_id=thread_id,
                owner_sha256=owner_sha256,
                fencing_generation=generation,
                acquired_at=acquired_at,
                expires_at=acquired_at + self._lease_duration,
            )
            if entry is None:
                entry = _SessionEntry(lease=lease, wake_pending=False)
                self._entries[thread_id] = entry
            else:
                entry.lease = lease
            return lease

    async def renew(
        self,
        lease: SessionWriterLease,
        *,
        renewed_at: datetime,
    ) -> SessionWriterLease:
        self._require_utc(renewed_at)
        async with self._lock:
            self._require_current(lease, renewed_at)
            renewed = lease.model_copy(
                update={"expires_at": renewed_at + self._lease_duration}
            )
            self._entries[lease.thread_id].lease = renewed
            return renewed

    async def validate_fence(
        self,
        lease: SessionWriterLease,
        *,
        applied_at: datetime,
    ) -> int:
        self._require_utc(applied_at)
        async with self._lock:
            self._require_current(lease, applied_at)
            return lease.fencing_generation

    async def request_wake(
        self,
        thread_id: ThreadId,
        *,
        requested_at: datetime,
    ) -> WakeDisposition:
        self._require_utc(requested_at)
        async with self._lock:
            self._prune_expired(requested_at)
            entry = self._entries.get(thread_id)
            if entry is None or entry.lease is None:
                self._reject(WriterCoordinatorErrorCode.NO_SESSION)
            if entry.wake_pending:
                return WakeDisposition.COALESCED
            entry.wake_pending = True
            return WakeDisposition.ENQUEUED

    async def take_wake(
        self,
        lease: SessionWriterLease,
        *,
        taken_at: datetime,
    ) -> bool:
        self._require_utc(taken_at)
        async with self._lock:
            entry = self._require_current(lease, taken_at)
            pending = entry.wake_pending
            entry.wake_pending = False
            return pending

    async def release(
        self,
        lease: SessionWriterLease,
        *,
        released_at: datetime,
    ) -> None:
        self._require_utc(released_at)
        async with self._lock:
            entry = self._require_matching(lease)
            entry.lease = None
            if not entry.wake_pending:
                del self._entries[lease.thread_id]

    async def active_sessions(self, *, observed_at: datetime) -> int:
        self._require_utc(observed_at)
        async with self._lock:
            self._prune_expired(observed_at)
            return self._active_count(observed_at)

    def _require_current(
        self,
        lease: SessionWriterLease,
        observed_at: datetime,
    ) -> _SessionEntry:
        entry = self._require_matching(lease)
        if lease.expires_at <= observed_at:
            self._reject(WriterCoordinatorErrorCode.FENCED)
        return entry

    def _require_matching(
        self,
        lease: SessionWriterLease,
    ) -> _SessionEntry:
        entry = self._entries.get(lease.thread_id)
        if entry is None or entry.lease != lease:
            self._reject(WriterCoordinatorErrorCode.FENCED)
        return entry

    def _active_count(self, observed_at: datetime) -> int:
        count = 0
        for entry in self._entries.values():
            if entry.lease is not None and entry.lease.expires_at > observed_at:
                count += 1
        return count

    def _prune_expired(self, observed_at: datetime) -> None:
        removable: list[ThreadId] = []
        for thread_id, entry in self._entries.items():
            if entry.lease is None or entry.lease.expires_at <= observed_at:
                entry.lease = None
                if not entry.wake_pending:
                    removable.append(thread_id)
        for thread_id in removable:
            del self._entries[thread_id]

    def _claim_generation(self) -> int:
        generation = self._next_generation
        if generation > 2**63 - 1:
            raise RuntimeError("writer fencing generation exhausted")
        self._next_generation += 1
        return generation

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("writer coordinator timestamp must use UTC")

    @staticmethod
    def _reject(code: WriterCoordinatorErrorCode) -> Never:
        raise WriterCoordinatorError(code)
