"""Fenced execution, heartbeat, artifacts, and terminal job outcomes."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta

from app.services.harness.protocol.background_job_execution import (
    BackgroundJobArtifactWriter,
    BackgroundJobClock,
    BackgroundJobCoordinatorError,
    BackgroundJobCoordinatorErrorCode,
    BackgroundJobExecutionResult,
    BackgroundJobExecutor,
    BackgroundJobStore,
)
from app.services.harness.protocol.background_job_updates import (
    claim_background_job,
    refresh_background_job,
    terminal_background_job,
)
from app.services.harness.protocol.background_jobs import (
    BackgroundJobConfiguration,
    BackgroundJobRecord,
    BackgroundJobState,
    JobExecutionOwnerId,
)


class BackgroundJobRunner:
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
        self._executor = executor
        self._artifacts = artifacts
        self._execution_owner_id = execution_owner_id
        self._configuration = configuration
        self._clock = clock
        self._active_cancellations: dict[str, asyncio.Event] = {}

    async def run_queued(self, queued: BackgroundJobRecord) -> None:
        current = await self._load_current(queued)
        if current.state is not BackgroundJobState.QUEUED:
            return
        observed_at = self._now()
        if observed_at >= current.queue_expires_at:
            await self._store.save(
                terminal_background_job(
                    current,
                    state=BackgroundJobState.FAILED,
                    observed_at=observed_at,
                    configuration=self._configuration,
                    status_reason="Background job expired before execution.",
                )
            )
            return
        claimed = await self._store.save(
            claim_background_job(
                current,
                execution_owner_id=self._execution_owner_id,
                observed_at=observed_at,
                configuration=self._configuration,
            )
        )
        cancellation = asyncio.Event()
        self._active_cancellations[claimed.operation_id] = cancellation
        try:
            await self._run_claimed(claimed, cancellation)
        finally:
            self._active_cancellations.pop(claimed.operation_id, None)

    def signal_cancellation(self, operation_id: str) -> None:
        cancellation = self._active_cancellations.get(operation_id)
        if cancellation is not None:
            cancellation.set()

    def signal_all_cancellations(self) -> None:
        for cancellation in self._active_cancellations.values():
            cancellation.set()

    async def _run_claimed(
        self,
        claimed: BackgroundJobRecord,
        cancellation: asyncio.Event,
    ) -> None:
        execution = await self._execute_with_heartbeat(
            claimed,
            cancellation,
        )
        self._validate_execution_result(claimed, execution)
        current = await self._load_current(claimed)
        if execution.status is BackgroundJobState.CANCELLED:
            await self._finish_cancelled(current)
            return
        if execution.status is BackgroundJobState.COMPLETED:
            await self._finish_completed(current, execution)
            return
        await self._finish_unsuccessful(current, execution.status)

    async def _execute_with_heartbeat(
        self,
        job: BackgroundJobRecord,
        cancellation: asyncio.Event,
    ) -> BackgroundJobExecutionResult:
        execution_task = asyncio.create_task(
            self._executor.execute(job, cancellation=cancellation),
            name=f"atlas-background-execution-{job.operation_id}",
        )
        cancellation_task = asyncio.create_task(
            cancellation.wait(),
            name=f"atlas-background-cancel-{job.operation_id}",
        )
        heartbeat_seconds = self._configuration.execution_lease_seconds / 2
        current = job
        try:
            while True:
                completed, _ = await asyncio.wait(
                    {execution_task, cancellation_task},
                    timeout=heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if execution_task in completed:
                    return await self._await_execution(execution_task)
                if cancellation_task in completed:
                    return await self._await_cancellation_grace(
                        execution_task
                    )
                current = await self._load_current(current)
                if current.state is BackgroundJobState.CANCELLING:
                    cancellation.set()
                    continue
                current = await self._store.save(
                    refresh_background_job(
                        current,
                        observed_at=self._now(),
                        configuration=self._configuration,
                    )
                )
        finally:
            cancellation_task.cancel()
            if not execution_task.done():
                execution_task.cancel()
            await asyncio.gather(
                execution_task,
                cancellation_task,
                return_exceptions=True,
            )

    @staticmethod
    async def _await_execution(
        execution_task: asyncio.Task[BackgroundJobExecutionResult],
    ) -> BackgroundJobExecutionResult:
        try:
            return await execution_task
        except asyncio.CancelledError as error:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            raise BackgroundJobCoordinatorError(
                BackgroundJobCoordinatorErrorCode.EXECUTION
            ) from error

    async def _await_cancellation_grace(
        self,
        execution_task: asyncio.Task[BackgroundJobExecutionResult],
    ) -> BackgroundJobExecutionResult:
        try:
            return await asyncio.wait_for(
                asyncio.shield(execution_task),
                timeout=self._configuration.cancellation_grace_seconds,
            )
        except TimeoutError as error:
            execution_task.cancel()
            await asyncio.gather(execution_task, return_exceptions=True)
            raise BackgroundJobCoordinatorError(
                BackgroundJobCoordinatorErrorCode.EXECUTION
            ) from error

    async def _finish_completed(
        self,
        job: BackgroundJobRecord,
        execution: BackgroundJobExecutionResult,
    ) -> None:
        if self._output_exceeds_bound(execution):
            await self._finish_output_rejected(job)
            return
        assert execution.result_bytes is not None
        result_artifact = await self._artifacts.write(
            job,
            kind="result",
            media_type=execution.result_media_type,
            content=execution.result_bytes,
        )
        log_artifact = None
        if execution.log_bytes is not None:
            log_artifact = await self._artifacts.write(
                job,
                kind="log",
                media_type=execution.log_media_type,
                content=execution.log_bytes,
            )
        await self._store.save(
            terminal_background_job(
                job,
                state=BackgroundJobState.COMPLETED,
                observed_at=self._now(),
                configuration=self._configuration,
                log_artifact=log_artifact,
                result_artifact=result_artifact,
            )
        )

    def _output_exceeds_bound(
        self,
        execution: BackgroundJobExecutionResult,
    ) -> bool:
        result_bytes = execution.result_bytes
        return (
            result_bytes is None
            or len(result_bytes) > self._configuration.maximum_result_bytes
            or (
            execution.log_bytes is not None
            and len(execution.log_bytes) > self._configuration.maximum_log_bytes
            )
        )

    async def _finish_cancelled(self, job: BackgroundJobRecord) -> None:
        current = await self._load_current(job)
        cancellation_requested_at = (
            current.cancellation_requested_at or self._now()
        )
        await self._store.save(
            terminal_background_job(
                current,
                state=BackgroundJobState.CANCELLED,
                observed_at=self._now(),
                configuration=self._configuration,
                status_reason="Background job execution was cancelled.",
                cancellation_requested_at=cancellation_requested_at,
            )
        )

    async def _finish_unsuccessful(
        self,
        job: BackgroundJobRecord,
        state: BackgroundJobState,
    ) -> None:
        current = await self._load_current(job)
        await self._store.save(
            terminal_background_job(
                current,
                state=state,
                observed_at=self._now(),
                configuration=self._configuration,
                status_reason="Background job execution was unsuccessful.",
            )
        )

    async def _finish_output_rejected(
        self,
        job: BackgroundJobRecord,
    ) -> None:
        current = await self._load_current(job)
        await self._store.save(
            terminal_background_job(
                current,
                state=BackgroundJobState.FAILED,
                observed_at=self._now(),
                configuration=self._configuration,
                status_reason=(
                    "Background job output exceeded its configured bound."
                ),
            )
        )

    @staticmethod
    def _validate_execution_result(
        job: BackgroundJobRecord,
        execution: BackgroundJobExecutionResult,
    ) -> None:
        operation = execution.operation
        if (
            operation.operation_id != job.operation_id
            or operation.tool_name != job.tool_name
            or operation.tool_version != job.tool_version
            or operation.args_sha256 != job.args_sha256
        ):
            raise BackgroundJobCoordinatorError(
                BackgroundJobCoordinatorErrorCode.EXECUTION
            )
        if execution.result_bytes is not None:
            result_sha256 = hashlib.sha256(execution.result_bytes).hexdigest()
            if operation.result_sha256 != result_sha256:
                raise BackgroundJobCoordinatorError(
                    BackgroundJobCoordinatorErrorCode.EXECUTION
                )

    async def _load_current(
        self,
        job: BackgroundJobRecord,
    ) -> BackgroundJobRecord:
        current = await self._store.load(
            job.workspace_id,
            job.owner_principal_id,
            job.operation_id,
        )
        if current is None:
            raise BackgroundJobCoordinatorError(
                BackgroundJobCoordinatorErrorCode.DURABILITY
            )
        return current

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise BackgroundJobCoordinatorError(
                BackgroundJobCoordinatorErrorCode.STATE
            )
        return value
