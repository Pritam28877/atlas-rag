"""Strict bounded protocol contracts for durable background tool jobs."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    ArtifactId,
    BoundedReason,
    MediaType,
    OperationId,
    PageInfo,
    PrincipalId,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.execution import ToolName, ToolVersion
from app.services.harness.protocol.provider_stream import ProviderCallId

MAXIMUM_BACKGROUND_JOBS = 4_096
MAXIMUM_BACKGROUND_JOB_PAGE = 200
MAXIMUM_BACKGROUND_JOB_BYTES = 16 * 1024 * 1024
MAXIMUM_QUEUE_TTL = timedelta(days=7)
MAXIMUM_RETENTION_TTL = timedelta(days=30)

type JobExecutionOwnerId = Annotated[
    str,
    StringConstraints(pattern=r"^jow_[0-9a-f]{32}$"),
]


class BackgroundJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AMBIGUOUS = "ambiguous"


TERMINAL_BACKGROUND_JOB_STATES = frozenset(
    {
        BackgroundJobState.COMPLETED,
        BackgroundJobState.FAILED,
        BackgroundJobState.CANCELLED,
        BackgroundJobState.AMBIGUOUS,
    }
)

BACKGROUND_JOB_TRANSITIONS = {
    BackgroundJobState.QUEUED: frozenset(
        {
            BackgroundJobState.RUNNING,
            BackgroundJobState.CANCELLED,
            BackgroundJobState.FAILED,
        }
    ),
    BackgroundJobState.RUNNING: frozenset(
        {
            BackgroundJobState.RUNNING,
            BackgroundJobState.CANCELLING,
            BackgroundJobState.COMPLETED,
            BackgroundJobState.FAILED,
            BackgroundJobState.AMBIGUOUS,
        }
    ),
    BackgroundJobState.CANCELLING: frozenset(
        {
            BackgroundJobState.CANCELLING,
            BackgroundJobState.CANCELLED,
            BackgroundJobState.FAILED,
            BackgroundJobState.AMBIGUOUS,
        }
    ),
    BackgroundJobState.COMPLETED: frozenset(),
    BackgroundJobState.FAILED: frozenset(),
    BackgroundJobState.CANCELLED: frozenset(),
    BackgroundJobState.AMBIGUOUS: frozenset(),
}


class BackgroundJobConfiguration(StrictProtocolModel):
    maximum_concurrent_jobs: int = Field(default=4, ge=1, le=256)
    maximum_queued_jobs: int = Field(
        default=64,
        ge=1,
        le=MAXIMUM_BACKGROUND_JOBS,
    )
    maximum_log_bytes: int = Field(
        default=1024 * 1024,
        ge=1,
        le=MAXIMUM_BACKGROUND_JOB_BYTES,
    )
    maximum_result_bytes: int = Field(
        default=1024 * 1024,
        ge=1,
        le=MAXIMUM_BACKGROUND_JOB_BYTES,
    )
    execution_lease_seconds: int = Field(default=30, ge=5, le=300)
    queue_ttl_seconds: int = Field(default=3_600, ge=60, le=604_800)
    retention_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    cleanup_batch_size: int = Field(default=128, ge=1, le=256)


class BackgroundJobArtifact(StrictProtocolModel):
    artifact_id: ArtifactId
    media_type: MediaType
    size_bytes: int = Field(ge=1, le=MAXIMUM_BACKGROUND_JOB_BYTES)
    content_sha256: Sha256


class BackgroundJobRecord(StrictProtocolModel):
    """One operation-owned job with fenced runtime ownership."""

    workspace_id: WorkspaceId
    operation_id: OperationId
    owner_principal_id: PrincipalId
    call_id: ProviderCallId
    tool_name: ToolName
    tool_version: ToolVersion
    descriptor_sha256: Sha256
    args_sha256: Sha256
    state: BackgroundJobState
    execution_generation: int = Field(default=0, ge=0, le=2_147_483_647)
    execution_owner_id: JobExecutionOwnerId | None = None
    execution_lease_expires_at: UtcTimestamp | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    queue_expires_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    cancellation_requested_at: UtcTimestamp | None = None
    terminal_at: UtcTimestamp | None = None
    retention_expires_at: UtcTimestamp | None = None
    log_artifact: BackgroundJobArtifact | None = None
    result_artifact: BackgroundJobArtifact | None = None
    status_reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("job update cannot precede creation")
        if self.queue_expires_at <= self.created_at:
            raise ValueError("job queue expiry must follow creation")
        if self.queue_expires_at - self.created_at > MAXIMUM_QUEUE_TTL:
            raise ValueError("job queue lifetime exceeds seven days")
        if (
            self.state is BackgroundJobState.QUEUED
            and self.updated_at > self.queue_expires_at
        ):
            raise ValueError("queued job cannot outlive its queue TTL")
        self._validate_execution_owner()
        self._validate_terminal_state()
        self._validate_cancellation()
        return self

    def _validate_execution_owner(self) -> None:
        is_active = self.state in {
            BackgroundJobState.RUNNING,
            BackgroundJobState.CANCELLING,
        }
        has_owner = (
            self.execution_owner_id is not None
            and self.execution_lease_expires_at is not None
        )
        if is_active != has_owner:
            raise ValueError("active job requires an execution owner and lease")
        if is_active and self.execution_generation == 0:
            raise ValueError("active job requires a positive fencing generation")
        if self.started_at is not None:
            if self.started_at < self.created_at:
                raise ValueError("job start cannot precede creation")
            if self.started_at > self.queue_expires_at:
                raise ValueError("job cannot start after its queue TTL")
            if self.execution_generation == 0:
                raise ValueError("started job requires a fencing generation")
        elif self.state not in {
            BackgroundJobState.QUEUED,
            BackgroundJobState.FAILED,
            BackgroundJobState.CANCELLED,
        }:
            raise ValueError("executed job state requires started_at")
        if (
            self.execution_lease_expires_at is not None
            and self.execution_lease_expires_at <= self.updated_at
        ):
            raise ValueError("active job execution lease must be unexpired")

    def _validate_terminal_state(self) -> None:
        is_terminal = self.state in TERMINAL_BACKGROUND_JOB_STATES
        terminal_metadata = (
            self.terminal_at is not None and self.retention_expires_at is not None
        )
        if is_terminal != terminal_metadata:
            raise ValueError("terminal job requires terminal retention metadata")
        if self.terminal_at is not None:
            if self.terminal_at < (self.started_at or self.created_at):
                raise ValueError("job termination cannot precede execution")
            if not self.terminal_at <= self.updated_at:
                raise ValueError("job update cannot precede termination")
            assert self.retention_expires_at is not None
            retention_ttl = self.retention_expires_at - self.terminal_at
            if not timedelta(0) < retention_ttl <= MAXIMUM_RETENTION_TTL:
                raise ValueError("job terminal retention exceeds thirty days")
        is_completed = self.state is BackgroundJobState.COMPLETED
        if is_completed != (self.result_artifact is not None):
            raise ValueError("completed job requires exactly one result artifact")
        needs_reason = self.state in {
            BackgroundJobState.FAILED,
            BackgroundJobState.CANCELLED,
            BackgroundJobState.AMBIGUOUS,
        }
        if needs_reason != (self.status_reason is not None):
            raise ValueError("unsuccessful terminal job requires a reason")

    def _validate_cancellation(self) -> None:
        requires_request = self.state in {
            BackgroundJobState.CANCELLING,
            BackgroundJobState.CANCELLED,
        }
        if requires_request and self.cancellation_requested_at is None:
            raise ValueError("cancelling job requires cancellation evidence")
        allows_request = self.state in {
            BackgroundJobState.CANCELLING,
            BackgroundJobState.CANCELLED,
            BackgroundJobState.FAILED,
            BackgroundJobState.AMBIGUOUS,
        }
        if self.cancellation_requested_at is not None and not allows_request:
            raise ValueError("job state cannot contain cancellation evidence")
        if self.cancellation_requested_at is not None:
            if not self.created_at <= self.cancellation_requested_at <= self.updated_at:
                raise ValueError("job cancellation timestamp is outside lifecycle")


class BackgroundJobPage(StrictProtocolModel):
    jobs: tuple[BackgroundJobRecord, ...] = Field(
        max_length=MAXIMUM_BACKGROUND_JOB_PAGE
    )
    page: PageInfo


def require_background_job_transition(
    current: BackgroundJobState,
    target: BackgroundJobState,
) -> None:
    if target not in BACKGROUND_JOB_TRANSITIONS[current]:
        raise ValueError(
            f"background job transition {current.value}->{target.value} is invalid"
        )
