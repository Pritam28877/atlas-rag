"""Fenced DAG scheduling contracts with bounded in-memory reference store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import Field

from app.services.harness.protocol.base import (
    BoundedLabel,
    Sha256,
    StrictProtocolModel,
    TaskId,
    UtcTimestamp,
)
from app.services.harness.scheduler.dag import TaskGraphDefinition, ready_task_ids

MAXIMUM_CLAIM_BATCH = 64


class DagNodeStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DagNodeRecord(StrictProtocolModel):
    task_id: TaskId
    status: DagNodeStatus
    owner_id: BoundedLabel | None = None
    lease_generation: int = Field(default=0, ge=0)
    lease_expires_at: UtcTimestamp | None = None
    result_sha256: Sha256 | None = None
    failure_reason: BoundedLabel | None = None


class DagLease(StrictProtocolModel):
    task_id: TaskId
    owner_id: BoundedLabel
    lease_generation: int = Field(ge=1)
    expires_at: UtcTimestamp


class DagStoreConflict(RuntimeError):
    pass


class DurableDagStore(Protocol):
    async def records(self) -> tuple[DagNodeRecord, ...]: ...

    async def claim(
        self,
        task_id: TaskId,
        owner_id: BoundedLabel,
        expires_at: datetime,
    ) -> DagLease: ...

    async def checkpoint(
        self,
        task_id: TaskId,
        owner_id: BoundedLabel,
        lease_generation: int,
        status: DagNodeStatus,
        *,
        result_sha256: Sha256 | None,
        failure_reason: BoundedLabel | None,
    ) -> DagNodeRecord: ...


class InMemoryDurableDagStore:
    """Reference store with durable-adapter ownership and fencing invariants."""

    def __init__(self, graph: TaskGraphDefinition) -> None:
        self._records = {
            node.task_id: DagNodeRecord(
                task_id=node.task_id,
                status=DagNodeStatus.READY,
            )
            for node in graph.nodes
        }
        self._lock = asyncio.Lock()

    async def records(self) -> tuple[DagNodeRecord, ...]:
        async with self._lock:
            return tuple(self._records.values())

    async def claim(
        self,
        task_id: TaskId,
        owner_id: BoundedLabel,
        expires_at: datetime,
    ) -> DagLease:
        async with self._lock:
            record = self._records.get(task_id)
            if record is None or record.status is not DagNodeStatus.READY:
                raise DagStoreConflict("DAG node is not ready")
            next_generation = record.lease_generation + 1
            lease = DagLease(
                task_id=task_id,
                owner_id=owner_id,
                lease_generation=next_generation,
                expires_at=expires_at,
            )
            self._records[task_id] = DagNodeRecord(
                task_id=task_id,
                status=DagNodeStatus.RUNNING,
                owner_id=owner_id,
                lease_generation=next_generation,
                lease_expires_at=expires_at,
            )
            return lease

    async def checkpoint(
        self,
        task_id: TaskId,
        owner_id: BoundedLabel,
        lease_generation: int,
        status: DagNodeStatus,
        *,
        result_sha256: Sha256 | None,
        failure_reason: BoundedLabel | None,
    ) -> DagNodeRecord:
        async with self._lock:
            record = self._records.get(task_id)
            if (
                record is None
                or record.status is not DagNodeStatus.RUNNING
                or record.owner_id != owner_id
                or record.lease_generation != lease_generation
                or status not in {DagNodeStatus.COMPLETED, DagNodeStatus.FAILED}
            ):
                raise DagStoreConflict("DAG lease fence rejected checkpoint")
            updated = DagNodeRecord(
                task_id=task_id,
                status=status,
                owner_id=None,
                lease_generation=lease_generation,
                result_sha256=result_sha256,
                failure_reason=failure_reason,
            )
            self._records[task_id] = updated
            return updated


class DurableDagScheduler:
    def __init__(self, graph: TaskGraphDefinition, store: DurableDagStore) -> None:
        self._graph = TaskGraphDefinition.model_validate(graph.model_dump())
        self._store = store

    async def claim_ready(
        self,
        owner_id: BoundedLabel,
        *,
        limit: int = MAXIMUM_CLAIM_BATCH,
        lease_duration_ms: int = 60_000,
        now: datetime,
    ) -> tuple[DagLease, ...]:
        if not 1 <= limit <= MAXIMUM_CLAIM_BATCH:
            raise ValueError("DAG claim limit exceeds bound")
        if not 100 <= lease_duration_ms <= 3_600_000:
            raise ValueError("DAG lease duration exceeds bound")
        records = await self._store.records()
        completed = frozenset(
            record.task_id
            for record in records
            if record.status is DagNodeStatus.COMPLETED
        )
        active = frozenset(
            record.task_id
            for record in records
            if record.status is not DagNodeStatus.READY
        )
        ready = ready_task_ids(self._graph, completed=completed, active=active)
        leases: list[DagLease] = []
        expires_at = now + timedelta(milliseconds=lease_duration_ms)
        for task_id in ready[:limit]:
            try:
                leases.append(await self._store.claim(task_id, owner_id, expires_at))
            except DagStoreConflict:
                continue
        return tuple(leases)

    async def checkpoint_success(
        self,
        lease: DagLease,
        result_sha256: Sha256,
    ) -> DagNodeRecord:
        return await self._store.checkpoint(
            lease.task_id,
            lease.owner_id,
            lease.lease_generation,
            DagNodeStatus.COMPLETED,
            result_sha256=result_sha256,
            failure_reason=None,
        )

    async def checkpoint_failure(
        self,
        lease: DagLease,
        reason: BoundedLabel,
    ) -> DagNodeRecord:
        return await self._store.checkpoint(
            lease.task_id,
            lease.owner_id,
            lease.lease_generation,
            DagNodeStatus.FAILED,
            result_sha256=None,
            failure_reason=reason,
        )
