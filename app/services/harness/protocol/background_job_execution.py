"""Interfaces and bounded results for owned background execution."""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    OperationId,
    OperationRecord,
    OperationState,
    PrincipalId,
    WorkspaceId,
)
from app.services.harness.protocol.background_jobs import (
    MAXIMUM_BACKGROUND_JOB_BYTES,
    BackgroundJobArtifact,
    BackgroundJobRecord,
    BackgroundJobState,
)
from app.services.harness.protocol.base import MediaType, StrictProtocolModel


class BackgroundJobCoordinatorErrorCode(StrEnum):
    CAPACITY = "capacity"
    CLOSED = "closed"
    DURABILITY = "durability"
    EXECUTION = "execution"
    OUTPUT = "output"
    STATE = "state"


class BackgroundJobCoordinatorError(RuntimeError):
    def __init__(self, code: BackgroundJobCoordinatorErrorCode) -> None:
        super().__init__("background job operation rejected")
        self.code = code


class BackgroundJobExecutionResult(StrictProtocolModel):
    status: BackgroundJobState
    operation: OperationRecord
    result_media_type: MediaType = "application/json"
    result_bytes: bytes | None = Field(
        default=None,
        min_length=1,
        max_length=MAXIMUM_BACKGROUND_JOB_BYTES,
        repr=False,
    )

    @model_validator(mode="after")
    def validate_terminal_operation(self) -> Self:
        expected_operation_state = {
            BackgroundJobState.COMPLETED: OperationState.COMPLETED,
            BackgroundJobState.FAILED: OperationState.FAILED,
            BackgroundJobState.CANCELLED: OperationState.CANCELLED,
            BackgroundJobState.AMBIGUOUS: OperationState.AMBIGUOUS,
        }.get(self.status)
        if expected_operation_state is None:
            raise ValueError("background execution result must be terminal")
        if self.operation.state is not expected_operation_state:
            raise ValueError("background result operation state disagrees")
        completed = self.status is BackgroundJobState.COMPLETED
        if completed != (self.result_bytes is not None):
            raise ValueError("completed background result requires output")
        return self
    log_media_type: MediaType = "text/plain"
    log_bytes: bytes | None = Field(
        default=None,
        min_length=1,
        max_length=MAXIMUM_BACKGROUND_JOB_BYTES,
        repr=False,
    )


class BackgroundJobExecutor(Protocol):
    async def execute(
        self,
        job: BackgroundJobRecord,
        *,
        cancellation: asyncio.Event,
    ) -> BackgroundJobExecutionResult: ...


class BackgroundJobArtifactWriter(Protocol):
    async def write(
        self,
        job: BackgroundJobRecord,
        *,
        kind: Literal["log", "result"],
        media_type: MediaType,
        content: bytes,
    ) -> BackgroundJobArtifact: ...


class BackgroundJobStore(Protocol):
    async def save(self, job: BackgroundJobRecord) -> BackgroundJobRecord: ...

    async def load(
        self,
        workspace_id: WorkspaceId,
        owner_principal_id: PrincipalId,
        operation_id: OperationId,
    ) -> BackgroundJobRecord | None: ...


class BackgroundJobClock(Protocol):
    def __call__(self) -> datetime: ...
