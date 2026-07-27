"""Owned bounded admission and workers for durable background jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.services.harness.protocol import OperationId, PrincipalId, WorkspaceId
from app.services.harness.protocol.background_job_execution import (
    BackgroundJobArtifactWriter,
    BackgroundJobClock,
    BackgroundJobCoordinatorError,
    BackgroundJobCoordinatorErrorCode,
    BackgroundJobExecutor,
    BackgroundJobStore,
)
from app.services.harness.protocol.background_job_updates import (
    request_background_job_cancellation,
)
from app.services.harness.protocol.background_jobs import (
    TERMINAL_BACKGROUND_JOB_STATES,
    BackgroundJobConfiguration,
    BackgroundJobRecord,
    BackgroundJobState,
    JobExecutionOwnerId,
)
from app.services.harness.scheduler.runner import BackgroundJobRunner

LOGGER = logging.getLogger(__name__)


class BackgroundJobCoordinator:
    """Owns every worker and bounded queued job reference."""

    def __init__(
        self,
        store: BackgroundJobStore,
        executor: BackgroundJobExecutor,
        artifacts: BackgroundJobArtifactWriter,
        *,
        execution_owner_id: JobExecutionOwnerId,
        configuration: BackgroundJobConfiguration,
        clock: BackgroundJobClock,
    ) -> None:
        self._store = store
        self._configuration = configuration
        self._clock = clock
        self._queue: asyncio.Queue[BackgroundJobRecord] = asyncio.Queue(
            maxsize=configuration.maximum_queued_jobs
        )
        self._submission_ids: set[str] = set()
        self._workers: tuple[asyncio.Task[None], ...] = ()
        self._admission_lock = asyncio.Lock()
        self._runner = BackgroundJobRunner(
            store,
            executor,
            artifacts,
            execution_owner_id=execution_owner_id,
            configuration=configuration,
            clock=clock,
        )
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            _reject(BackgroundJobCoordinatorErrorCode.CLOSED)
        if self._started:
            _reject(BackgroundJobCoordinatorErrorCode.STATE)
        self._started = True
        self._workers = tuple(
            asyncio.create_task(
                self._worker(),
                name=f"atlas-background-job-{worker_number}",
            )
            for worker_number in range(
                self._configuration.maximum_concurrent_jobs
            )
        )

    async def submit(self, job: BackgroundJobRecord) -> BackgroundJobRecord:
        self._require_running()
        if (
            job.state is not BackgroundJobState.QUEUED
            or job.queue_expires_at - job.created_at
            > timedelta(seconds=self._configuration.queue_ttl_seconds)
        ):
            _reject(BackgroundJobCoordinatorErrorCode.STATE)
        await self._reserve_submission(job.operation_id)
        try:
            existing = await self._store.load(
                job.workspace_id,
                job.owner_principal_id,
                job.operation_id,
            )
            if existing is not None:
                await self._release_submission(job.operation_id)
                return existing
            durable = await self._store.save(job)
            self._queue.put_nowait(durable)
            return durable
        except BaseException:
            await self._release_submission(job.operation_id)
            raise

    async def status(
        self,
        workspace_id: WorkspaceId,
        owner_principal_id: PrincipalId,
        operation_id: OperationId,
    ) -> BackgroundJobRecord | None:
        return await self._store.load(
            workspace_id,
            owner_principal_id,
            operation_id,
        )

    async def resume_durable(self, job: BackgroundJobRecord) -> None:
        self._require_running()
        if job.state is not BackgroundJobState.QUEUED:
            _reject(BackgroundJobCoordinatorErrorCode.STATE)
        await self._reserve_submission(job.operation_id)
        try:
            self._queue.put_nowait(job)
        except BaseException:
            await self._release_submission(job.operation_id)
            raise

    async def cancel(
        self,
        workspace_id: WorkspaceId,
        owner_principal_id: PrincipalId,
        operation_id: OperationId,
    ) -> BackgroundJobRecord | None:
        self._require_running()
        job = await self._store.load(
            workspace_id,
            owner_principal_id,
            operation_id,
        )
        if job is None or job.state in TERMINAL_BACKGROUND_JOB_STATES:
            return job
        if job.state is BackgroundJobState.CANCELLING:
            self._runner.signal_cancellation(operation_id)
            return job
        durable = await self._store.save(
            request_background_job_cancellation(
                job,
                observed_at=self._now(),
                configuration=self._configuration,
            )
        )
        self._runner.signal_cancellation(operation_id)
        return durable

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._runner.signal_all_cancellations()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = ()
        while not self._queue.empty():
            queued = self._queue.get_nowait()
            self._submission_ids.discard(queued.operation_id)
            self._queue.task_done()
        self._submission_ids.clear()

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            await self._release_submission(job.operation_id)
            try:
                await self._runner.run_queued(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "background job worker failed",
                    extra={
                        "workspace_id": job.workspace_id,
                        "operation_id": job.operation_id,
                    },
                )
            finally:
                self._queue.task_done()

    async def _reserve_submission(self, operation_id: str) -> None:
        async with self._admission_lock:
            if operation_id in self._submission_ids:
                _reject(BackgroundJobCoordinatorErrorCode.STATE)
            if (
                len(self._submission_ids)
                >= self._configuration.maximum_queued_jobs
            ):
                _reject(BackgroundJobCoordinatorErrorCode.CAPACITY)
            self._submission_ids.add(operation_id)

    async def _release_submission(self, operation_id: str) -> None:
        async with self._admission_lock:
            self._submission_ids.discard(operation_id)

    def _require_running(self) -> None:
        if self._closed:
            _reject(BackgroundJobCoordinatorErrorCode.CLOSED)
        if not self._started:
            _reject(BackgroundJobCoordinatorErrorCode.STATE)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            _reject(BackgroundJobCoordinatorErrorCode.STATE)
        return value


def _reject(code: BackgroundJobCoordinatorErrorCode) -> None:
    raise BackgroundJobCoordinatorError(code)
