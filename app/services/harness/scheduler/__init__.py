"""Validated and bounded Atlas Harness task scheduling."""

from app.services.harness.protocol.background_jobs import (
    BACKGROUND_JOB_TRANSITIONS,
    MAXIMUM_BACKGROUND_JOB_BYTES,
    MAXIMUM_BACKGROUND_JOB_PAGE,
    MAXIMUM_BACKGROUND_JOBS,
    TERMINAL_BACKGROUND_JOB_STATES,
    BackgroundJobArtifact,
    BackgroundJobConfiguration,
    BackgroundJobPage,
    BackgroundJobRecord,
    BackgroundJobState,
    JobExecutionOwnerId,
    require_background_job_transition,
)

__all__ = (
    "BACKGROUND_JOB_TRANSITIONS",
    "MAXIMUM_BACKGROUND_JOB_BYTES",
    "MAXIMUM_BACKGROUND_JOB_PAGE",
    "MAXIMUM_BACKGROUND_JOBS",
    "TERMINAL_BACKGROUND_JOB_STATES",
    "BackgroundJobArtifact",
    "BackgroundJobConfiguration",
    "BackgroundJobPage",
    "BackgroundJobRecord",
    "BackgroundJobState",
    "JobExecutionOwnerId",
    "require_background_job_transition",
)
