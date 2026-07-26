"""Content-addressed artifact metadata and lifecycle evidence."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ArtifactId,
    BoundedReason,
    MediaType,
    Sha256,
    StrictProtocolModel,
    TaskId,
    TurnId,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.conversation import DataClassification
from app.services.harness.protocol.states import ArtifactState


class ArtifactRecord(StrictProtocolModel):
    """Bounded metadata for immutable content stored outside the event journal."""

    artifact_id: ArtifactId
    workspace_id: WorkspaceId
    turn_id: TurnId | None = None
    task_id: TaskId | None = None
    media_type: MediaType
    size_bytes: int = Field(ge=1, le=4 * 1024 * 1024 * 1024)
    content_sha256: Sha256
    storage_reference_sha256: Sha256
    classification: DataClassification
    state: ArtifactState
    created_at: UtcTimestamp
    state_changed_at: UtcTimestamp
    durable_at: UtcTimestamp | None = None
    deleted_at: UtcTimestamp | None = None
    status_reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.state_changed_at < self.created_at:
            raise ValueError("artifact state change cannot precede creation")
        if self.state is ArtifactState.DURABLE and self.durable_at is None:
            raise ValueError("durable artifact state requires durable_at")
        if self.state in {ArtifactState.STAGED, ArtifactState.QUARANTINED}:
            if self.durable_at is not None:
                raise ValueError("non-durable artifact cannot contain durable_at")
        is_deleted = self.state is ArtifactState.DELETED
        if is_deleted != (self.deleted_at is not None):
            raise ValueError("deleted artifact state requires deleted_at")
        if self.durable_at is not None:
            if not self.created_at <= self.durable_at <= self.state_changed_at:
                raise ValueError("artifact durability time is outside its lifecycle")
            if self.state is ArtifactState.DURABLE:
                if self.durable_at != self.state_changed_at:
                    raise ValueError("durable_at must match the durable state change")
        if self.deleted_at is not None:
            if self.deleted_at != self.state_changed_at:
                raise ValueError("artifact deletion time must match its state change")
        needs_reason = self.state in {
            ArtifactState.QUARANTINED,
            ArtifactState.DELETED,
        }
        if needs_reason != (self.status_reason is not None):
            raise ValueError("quarantined or deleted artifact requires a status reason")
        return self
