"""Backend-neutral persistence contracts for projection checkpoints."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.journal.projection_contracts import (
    ProjectionCheckpoint,
    ProjectionName,
)
from app.services.harness.protocol import StrictProtocolModel, UtcTimestamp, WorkspaceId

FailureCode = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]+$",
    ),
]


class ProjectionHealth(StrEnum):
    HEALTHY = "healthy"
    DIVERGED = "diverged"
    NEEDS_OPERATOR = "needs_operator"


class ProjectionStoreConflict(RuntimeError):
    """A checkpoint changed after it was read."""

    def __init__(self) -> None:
        super().__init__("projection checkpoint conflict")


class ProjectionExpectation(StrictProtocolModel):
    generation: int | None = Field(default=None, ge=1, le=2**63 - 1)
    last_journal_sequence: int | None = Field(
        default=None,
        ge=0,
        le=2**63 - 1,
    )

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if (self.generation is None) != (self.last_journal_sequence is None):
            raise ValueError("projection expectation fields must be paired")
        return self


class StoredProjection(StrictProtocolModel):
    generation: int = Field(ge=1, le=2**63 - 1)
    checkpoint: ProjectionCheckpoint
    health: ProjectionHealth
    failure_code: FailureCode | None = None
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_health(self) -> Self:
        if (self.health is ProjectionHealth.HEALTHY) != (
            self.failure_code is None
        ):
            raise ValueError("projection health and failure code disagree")
        return self


class ProjectionStore(Protocol):
    async def load(
        self,
        workspace_id: WorkspaceId,
        projection_name: ProjectionName,
    ) -> StoredProjection | None: ...

    async def save_online(
        self,
        checkpoint: ProjectionCheckpoint,
        expected: ProjectionExpectation,
    ) -> StoredProjection: ...

    async def replace_rebuild(
        self,
        checkpoint: ProjectionCheckpoint,
        expected: ProjectionExpectation,
    ) -> StoredProjection: ...

    async def mark_unhealthy(
        self,
        workspace_id: WorkspaceId,
        projection_name: ProjectionName,
        health: ProjectionHealth,
        failure_code: FailureCode,
        expected: ProjectionExpectation,
    ) -> StoredProjection: ...


def projection_expectation(
    stored: StoredProjection | None,
) -> ProjectionExpectation:
    if stored is None:
        return ProjectionExpectation()
    return ProjectionExpectation(
        generation=stored.generation,
        last_journal_sequence=stored.checkpoint.last_journal_sequence,
    )
