"""Validated lifecycle updates for durable background jobs."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.harness.protocol.background_jobs import (
    BackgroundJobArtifact,
    BackgroundJobConfiguration,
    BackgroundJobRecord,
    BackgroundJobState,
)


def claim_background_job(
    job: BackgroundJobRecord,
    *,
    execution_owner_id: str,
    observed_at: datetime,
    configuration: BackgroundJobConfiguration,
) -> BackgroundJobRecord:
    return _updated(
        job,
        state=BackgroundJobState.RUNNING,
        execution_generation=job.execution_generation + 1,
        execution_owner_id=execution_owner_id,
        execution_lease_expires_at=observed_at
        + timedelta(seconds=configuration.execution_lease_seconds),
        started_at=observed_at,
        updated_at=observed_at,
    )


def refresh_background_job(
    job: BackgroundJobRecord,
    *,
    observed_at: datetime,
    configuration: BackgroundJobConfiguration,
) -> BackgroundJobRecord:
    return _updated(
        job,
        updated_at=observed_at,
        execution_lease_expires_at=observed_at
        + timedelta(seconds=configuration.execution_lease_seconds),
    )


def request_background_job_cancellation(
    job: BackgroundJobRecord,
    *,
    observed_at: datetime,
    configuration: BackgroundJobConfiguration,
) -> BackgroundJobRecord:
    if job.state is BackgroundJobState.QUEUED:
        return terminal_background_job(
            job,
            state=BackgroundJobState.CANCELLED,
            observed_at=observed_at,
            configuration=configuration,
            status_reason="Background job was cancelled before execution.",
            cancellation_requested_at=observed_at,
        )
    return _updated(
        job,
        state=BackgroundJobState.CANCELLING,
        cancellation_requested_at=observed_at,
        updated_at=observed_at,
    )


def terminal_background_job(
    job: BackgroundJobRecord,
    *,
    state: BackgroundJobState,
    observed_at: datetime,
    configuration: BackgroundJobConfiguration,
    status_reason: str | None = None,
    cancellation_requested_at: datetime | None = None,
    log_artifact: BackgroundJobArtifact | None = None,
    result_artifact: BackgroundJobArtifact | None = None,
) -> BackgroundJobRecord:
    return _updated(
        job,
        state=state,
        execution_owner_id=None,
        execution_lease_expires_at=None,
        updated_at=observed_at,
        cancellation_requested_at=(
            cancellation_requested_at or job.cancellation_requested_at
        ),
        terminal_at=observed_at,
        retention_expires_at=observed_at
        + timedelta(seconds=configuration.retention_ttl_seconds),
        log_artifact=log_artifact,
        result_artifact=result_artifact,
        status_reason=status_reason,
    )


def _updated(
    job: BackgroundJobRecord,
    **changes: object,
) -> BackgroundJobRecord:
    values = job.model_dump(mode="python")
    values.update(changes)
    return BackgroundJobRecord.model_validate(values)
