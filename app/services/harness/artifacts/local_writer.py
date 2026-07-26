"""Admission-controlled local blob publishing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from app.services.harness.artifacts.contracts import (
    BlobWriteRequest,
    BlobWriteResult,
)
from app.services.harness.artifacts.local import LocalBlobStore
from app.services.harness.protocol import Sha256, WorkspaceId
from app.services.harness.protocol.retention import (
    BlobReservation,
    RetainedBlob,
    RetentionPolicy,
    StorageReservationDecision,
    StorageSealReason,
)


class StorageAdmissionError(RuntimeError):
    def __init__(self, reason: StorageSealReason) -> None:
        super().__init__("artifact storage admission denied")
        self.reason = reason


class StorageReservationStore(Protocol):
    async def reserve(
        self,
        policy: RetentionPolicy,
        workspace_id: WorkspaceId,
        content_sha256: Sha256,
        size_bytes: int,
        *,
        available_filesystem_bytes: int,
        reserved_at: datetime,
    ) -> StorageReservationDecision: ...

    async def commit(
        self,
        reservation: BlobReservation,
        *,
        committed_at: datetime,
    ) -> RetainedBlob: ...

    async def release(
        self,
        reservation: BlobReservation,
        *,
        released_at: datetime,
    ) -> BlobReservation: ...


class AdmissionControlledBlobWriter:
    def __init__(
        self,
        workspace_id: WorkspaceId,
        policy: RetentionPolicy,
        blob_store: LocalBlobStore,
        reservation_store: StorageReservationStore,
        *,
        capacity_reader: Callable[[], Awaitable[int]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._workspace_id = workspace_id
        self._policy = policy
        self._blob_store = blob_store
        self._reservation_store = reservation_store
        self._capacity_reader = capacity_reader or blob_store.available_bytes
        self._clock = clock

    async def put(
        self,
        request: BlobWriteRequest,
        chunks: AsyncIterable[bytes],
    ) -> BlobWriteResult:
        available_bytes = await self._capacity_reader()
        reserved_at = self._clock()
        decision = await self._reservation_store.reserve(
            self._policy,
            self._workspace_id,
            request.expected_content_sha256,
            request.expected_size_bytes,
            available_filesystem_bytes=available_bytes,
            reserved_at=reserved_at,
        )
        if not decision.admission.allowed:
            if decision.admission.reason is None:
                raise RuntimeError("storage denial omitted its reason")
            raise StorageAdmissionError(decision.admission.reason)
        reservation = decision.reservation
        if reservation is None:
            raise RuntimeError("storage admission omitted its reservation")
        try:
            result = await self._blob_store.put(request, chunks)
        except asyncio.CancelledError:
            if not decision.already_committed:
                await asyncio.shield(self._release(reservation))
            raise
        except BaseException:
            if not decision.already_committed:
                await self._release(reservation)
            raise
        if not decision.already_committed:
            await self._reservation_store.commit(
                reservation,
                committed_at=self._clock(),
            )
        return result

    async def _release(self, reservation: BlobReservation) -> None:
        await self._reservation_store.release(
            reservation,
            released_at=self._clock(),
        )
