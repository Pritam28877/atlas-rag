"""Strict bounded domain contracts for artifact retention."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
    WorkspaceId,
)

GIBIBYTE = 1024 * 1024 * 1024
MAXIMUM_TRACKED_BLOBS = 10_000
MAXIMUM_GC_CANDIDATES = 1_000
MAXIMUM_SNAPSHOT_SEGMENTS = 1_000


class StorageSealReason(StrEnum):
    ALREADY_SEALED = "already_sealed"
    DISK_RESERVE = "disk_reserve"
    WORKSPACE_QUOTA = "workspace_quota"


class BlobReservationStatus(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"


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
    released_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_release(self) -> Self:
        if self.released_at is not None and self.released_at <= self.created_at:
            raise ValueError("artifact reference release must follow creation")
        return self

    @property
    def active(self) -> bool:
        return self.released_at is None


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


class SealedJournalSegment(StrictProtocolModel):
    workspace_id: WorkspaceId
    segment_sha256: Sha256
    snapshot_sha256: Sha256
    first_journal_sequence: int = Field(ge=1, le=2**63 - 1)
    last_journal_sequence: int = Field(ge=1, le=2**63 - 1)
    event_count: int = Field(ge=1, le=2**31 - 1)
    sealed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_sequence_range(self) -> Self:
        if self.last_journal_sequence < self.first_journal_sequence:
            raise ValueError("sealed segment sequence range is reversed")
        sequence_width = (
            self.last_journal_sequence - self.first_journal_sequence + 1
        )
        if self.event_count > sequence_width:
            raise ValueError("sealed segment event count exceeds sequence range")
        return self


class SyncedSnapshotBundle(StrictProtocolModel):
    snapshot: SyncedSnapshot
    segments: tuple[SealedJournalSegment, ...] = Field(
        max_length=MAXIMUM_SNAPSHOT_SEGMENTS
    )

    @model_validator(mode="after")
    def validate_segments(self) -> Self:
        previous_last_sequence = 0
        observed_hashes: set[str] = set()
        for segment in self.segments:
            if (
                segment.workspace_id != self.snapshot.workspace_id
                or segment.snapshot_sha256 != self.snapshot.snapshot_sha256
            ):
                raise ValueError("sealed segment scope does not match snapshot")
            if segment.segment_sha256 in observed_hashes:
                raise ValueError("sealed segment hashes must be unique")
            if segment.first_journal_sequence <= previous_last_sequence:
                raise ValueError("sealed segments must be sorted and non-overlapping")
            if (
                segment.last_journal_sequence
                > self.snapshot.through_journal_sequence
            ):
                raise ValueError("sealed segment exceeds snapshot replay baseline")
            if segment.sealed_at > self.snapshot.synced_at:
                raise ValueError("sealed segment cannot postdate its snapshot")
            observed_hashes.add(segment.segment_sha256)
            previous_last_sequence = segment.last_journal_sequence
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
    garbage_collected_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_tombstone_scope(self) -> Self:
        if self.tombstone is None:
            if self.garbage_collected_at is not None:
                raise ValueError("collected artifact requires a tombstone")
        elif (
            self.tombstone.workspace_id != self.workspace_id
            or self.tombstone.content_sha256 != self.content_sha256
        ):
            raise ValueError("artifact tombstone scope does not match blob")
        if (
            self.garbage_collected_at is not None
            and self.garbage_collected_at <= self.created_at
        ):
            raise ValueError("artifact collection must follow creation")
        return self


class RetentionEvidence(StrictProtocolModel):
    workspace_id: WorkspaceId
    blobs: tuple[RetainedBlob, ...] = Field(max_length=MAXIMUM_TRACKED_BLOBS)
    references: tuple[ArtifactReference, ...] = Field(
        max_length=MAXIMUM_TRACKED_BLOBS
    )
    legal_holds: tuple[ArtifactLegalHold, ...] = Field(
        max_length=MAXIMUM_TRACKED_BLOBS
    )

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        for blob in self.blobs:
            if blob.workspace_id != self.workspace_id:
                raise ValueError("retention evidence must use one workspace")
        for reference in self.references:
            if reference.workspace_id != self.workspace_id:
                raise ValueError("retention evidence must use one workspace")
        for legal_hold in self.legal_holds:
            if legal_hold.workspace_id != self.workspace_id:
                raise ValueError("retention evidence must use one workspace")
        return self


class WorkspaceStorageState(StrictProtocolModel):
    workspace_id: WorkspaceId
    used_blob_bytes: int = Field(ge=0, le=64 * GIBIBYTE)
    reserved_blob_bytes: int = Field(default=0, ge=0, le=64 * GIBIBYTE)
    available_filesystem_bytes: int = Field(ge=0, le=2**63 - 1)
    sealed: bool = False

    @model_validator(mode="after")
    def validate_total_usage(self) -> Self:
        if self.used_blob_bytes + self.reserved_blob_bytes > 64 * GIBIBYTE:
            raise ValueError("workspace storage accounting exceeds maximum")
        return self


class BlobReservation(StrictProtocolModel):
    workspace_id: WorkspaceId
    content_sha256: Sha256
    size_bytes: int = Field(ge=1, le=4 * GIBIBYTE)
    status: BlobReservationStatus
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("blob reservation update precedes creation")
        return self


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


class StorageReservationDecision(StrictProtocolModel):
    admission: WriteAdmission
    reservation: BlobReservation | None = None
    already_committed: bool = False

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.admission.allowed != (self.reservation is not None):
            raise ValueError("storage reservation decision is inconsistent")
        if self.already_committed and (
            self.reservation is None
            or self.reservation.status is not BlobReservationStatus.COMMITTED
        ):
            raise ValueError("committed reservation decision is inconsistent")
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
