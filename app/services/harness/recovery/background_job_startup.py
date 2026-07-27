"""Bounded fail-closed startup reconciliation for background jobs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from pydantic import Field

from app.services.harness.protocol import (
    OperationRecord,
    OperationState,
    StrictProtocolModel,
)
from app.services.harness.protocol.background_job_updates import (
    terminal_background_job,
)
from app.services.harness.protocol.background_jobs import (
    BackgroundJobConfiguration,
    BackgroundJobRecord,
    BackgroundJobState,
)
from app.services.harness.runtime import classify_operation_recovery


class BackgroundJobStartupStore(Protocol):
    async def load_recoverable(
        self,
        *,
        maximum_records: int,
    ) -> tuple[BackgroundJobRecord, ...]: ...

    async def save(self, job: BackgroundJobRecord) -> BackgroundJobRecord: ...


class BackgroundJobOperationStore(Protocol):
    async def load_operation(
        self,
        workspace_id: str,
        operation_id: str,
    ) -> OperationRecord | None: ...

    async def save_operation(
        self,
        workspace_id: str,
        operation: OperationRecord,
        *,
        updated_at: datetime,
    ) -> OperationRecord: ...


class BackgroundJobStartupReport(StrictProtocolModel):
    jobs_checked: int = Field(ge=0, le=256)
    resumable_jobs: tuple[BackgroundJobRecord, ...] = Field(max_length=256)
    terminalized_jobs: int = Field(ge=0, le=256)
    active_jobs: int = Field(ge=0, le=256)


class BackgroundJobStartupRecovery:
    def __init__(
        self,
        job_store: BackgroundJobStartupStore,
        operation_store: BackgroundJobOperationStore,
        configuration: BackgroundJobConfiguration,
    ) -> None:
        self._job_store = job_store
        self._operation_store = operation_store
        self._configuration = configuration

    async def recover(
        self,
        *,
        recovered_at: datetime,
        maximum_records: int = 256,
    ) -> BackgroundJobStartupReport:
        _require_utc(recovered_at)
        if not 1 <= maximum_records <= min(
            256,
            self._configuration.maximum_queued_jobs,
        ):
            raise ValueError("background recovery batch exceeds queue capacity")
        jobs = await self._job_store.load_recoverable(
            maximum_records=maximum_records
        )
        resumable: list[BackgroundJobRecord] = []
        terminalized = 0
        active = 0
        for job in jobs:
            if job.state is BackgroundJobState.QUEUED:
                operation = await self._operation_store.load_operation(
                    job.workspace_id,
                    job.operation_id,
                )
                if operation is None:
                    raise RuntimeError("background recovery operation is missing")
                if (
                    recovered_at < job.queue_expires_at
                    and operation.state is OperationState.DISPATCHED
                ):
                    resumable.append(job)
                else:
                    state = (
                        BackgroundJobState.AMBIGUOUS
                        if operation.state is OperationState.AMBIGUOUS
                        else BackgroundJobState.FAILED
                    )
                    await self._terminalize(
                        job,
                        state,
                        recovered_at,
                        "Background job expired before startup recovery.",
                    )
                    terminalized += 1
                continue
            lease_expires_at = job.execution_lease_expires_at
            if lease_expires_at is not None and lease_expires_at > recovered_at:
                active += 1
                continue
            await self._recover_interrupted(job, recovered_at)
            terminalized += 1
        return BackgroundJobStartupReport(
            jobs_checked=len(jobs),
            resumable_jobs=tuple(resumable),
            terminalized_jobs=terminalized,
            active_jobs=active,
        )

    async def _recover_interrupted(
        self,
        job: BackgroundJobRecord,
        recovered_at: datetime,
    ) -> None:
        operation = await self._operation_store.load_operation(
            job.workspace_id,
            job.operation_id,
        )
        if operation is None:
            raise RuntimeError("background recovery operation is missing")
        if operation.state is OperationState.DISPATCHED:
            decision = classify_operation_recovery(
                operation,
                recovered_at=recovered_at,
            )
            operation = await self._operation_store.save_operation(
                job.workspace_id,
                decision.operation,
                updated_at=recovered_at,
            )
        if operation.state is OperationState.AMBIGUOUS:
            state = BackgroundJobState.AMBIGUOUS
            reason = "Interrupted background operation requires reconciliation."
        else:
            state = BackgroundJobState.FAILED
            reason = "Interrupted background job has no recoverable result."
        await self._terminalize(job, state, recovered_at, reason)

    async def _terminalize(
        self,
        job: BackgroundJobRecord,
        state: BackgroundJobState,
        recovered_at: datetime,
        reason: str,
    ) -> None:
        await self._job_store.save(
            terminal_background_job(
                job,
                state=state,
                observed_at=recovered_at,
                configuration=self._configuration,
                status_reason=reason,
            )
        )


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("background recovery timestamp must use UTC")
