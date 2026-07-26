"""Strict bounded evidence and policy contracts for artifact retention."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.base import BoundedReason

GIBIBYTE = 1024 * 1024 * 1024
MAXIMUM_TRACKED_BLOBS = 10_000
MAXIMUM_GC_CANDIDATES = 1_000


class StorageSealReason(StrEnum):
    ALREADY_SEALED = "already_sealed"
    DISK_RESERVE = "disk_reserve"
    WORKSPACE_QUOTA = "workspace_quota"


class RetentionPolicy(StrictProtocolModel):
    retention_days: int = Field(default=90, ge=1, le=3650)
    blob_quota_bytes: int = Field(
        default=2 * GIBIBYTE,
        ge=1024 * 1024,
        le=64 * GIBIBYTE,
    )
    disk_reserve_bytes: int = Field(
        ge=1024 * 1024,
        le=64 * GIBIBYTE,
    )
    garbage_collection_grace_seconds: int = Field(
        default=24 * 60 * 60,
        ge=60 * 60,
        le=30 * 24 * 60 * 60,
    )
    maximum_gc_candidates: int = Field(
        default=250,
        ge=1,
        le=MAXIMUM_GC_CANDIDATES,
    )
    maximum_gc_bytes: int = Field(
        default=GIBIBYTE,
        ge=1024 * 1024,
        le=64 * GIBIBYTE,
    )


class ArtifactReference(StrictProtocolModel):
    workspace_id: WorkspaceId
    reference_sha256: Sha256
    content_sha256: Sha256
    created_at: UtcTimestamp


class SyncedSnapshot(StrictProtocolModel):
    workspace_id: WorkspaceId
    snapshot_sha256: Sha256
    manifest_sha256: Sha256
    through_journal_sequence: int = Field(ge=0, le=2**63 - 1)
    reachable_content_sha256s: tuple[Sha256, ...] = Field(
        max_length=MAXIMUM_TRACKED_BLOBS
    )
    synced_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_reachability(self) -> Self:
        if (
            tuple(sorted(set(self.reachable_content_sha256s)))
            != self.reachable_content_sha256s
        ):
            raise ValueError("snapshot reachable hashes must be unique and sorted")
        return self


class ArtifactTombstone(StrictProtocolModel):
    workspace_id: WorkspaceId
    content_sha256: Sha256
    tombstoned_at: UtcTimestamp
    delete_after: UtcTimestamp
    reason: BoundedReason

    @model_validator(mode="after")
    def validate_grace(self) -> Self:
        if self.delete_after <= self.tombstoned_at:
            raise ValueError("artifact tombstone requires a future delete time")
        return self


class ArtifactLegalHold(StrictProtocolModel):
    workspace_id: WorkspaceId
    hold_sha256: Sha256
    content_sha256: Sha256
    placed_at: UtcTimestamp
    released_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_release(self) -> Self:
        if self.released_at is not None and self.released_at <= self.placed_at:
            raise ValueError("artifact legal hold release must follow placement")
        return self

    @property
    def active(self) -> bool:
        return self.released_at is None


class RetainedBlob(StrictProtocolModel):
    workspace_id: WorkspaceId
    content_sha256: Sha256
    size_bytes: int = Field(ge=1, le=4 * GIBIBYTE)
    created_at: UtcTimestamp
    tombstone: ArtifactTombstone | None = None

    @model_validator(mode="after")
    def validate_tombstone_scope(self) -> Self:
        if self.tombstone is None:
            return self
        if (
            self.tombstone.workspace_id != self.workspace_id
            or self.tombstone.content_sha256 != self.content_sha256
        ):
            raise ValueError("artifact tombstone scope does not match blob")
        return self


class WorkspaceStorageState(StrictProtocolModel):
    workspace_id: WorkspaceId
    used_blob_bytes: int = Field(ge=0, le=64 * GIBIBYTE)
    available_filesystem_bytes: int = Field(ge=0, le=2**63 - 1)
    sealed: bool = False


class WriteAdmission(StrictProtocolModel):
    allowed: bool
    seal_required: bool
    reason: StorageSealReason | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.allowed == self.seal_required:
            raise ValueError("storage admission decision is inconsistent")
        if self.allowed != (self.reason is None):
            raise ValueError("storage admission reason is inconsistent")
        return self


class GarbageCollectionPlan(StrictProtocolModel):
    workspace_id: WorkspaceId
    snapshot_sha256: Sha256
    candidate_content_sha256s: tuple[Sha256, ...] = Field(
        max_length=MAXIMUM_GC_CANDIDATES
    )
    reclaimable_bytes: int = Field(ge=0, le=64 * GIBIBYTE)
    complete: bool
    planned_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        if len(set(self.candidate_content_sha256s)) != len(
            self.candidate_content_sha256s
        ):
            raise ValueError("garbage collection candidates must be unique")
        if bool(self.candidate_content_sha256s) != (
            self.reclaimable_bytes > 0
        ):
            raise ValueError("garbage collection bytes and candidates disagree")
        return self
