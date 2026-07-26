"""Deterministic fail-closed artifact retention decisions."""

from datetime import datetime, timedelta

from pydantic import TypeAdapter

from app.services.harness.protocol import UtcTimestamp
from app.services.harness.protocol.retention import (
    MAXIMUM_TRACKED_BLOBS,
    ArtifactLegalHold,
    ArtifactReference,
    GarbageCollectionPlan,
    RetainedBlob,
    RetentionPolicy,
    StorageSealReason,
    SyncedSnapshot,
    WorkspaceStorageState,
    WriteAdmission,
)

_UTC_TIMESTAMP_ADAPTER = TypeAdapter(UtcTimestamp)


def admit_blob_write(
    policy: RetentionPolicy,
    state: WorkspaceStorageState,
    incoming_bytes: int,
) -> WriteAdmission:
    if not 1 <= incoming_bytes <= 4 * 1024 * 1024 * 1024:
        raise ValueError("incoming blob bytes must be between 1 and 4294967296")
    if state.sealed:
        return _sealed(StorageSealReason.ALREADY_SEALED)
    if state.used_blob_bytes + incoming_bytes > policy.blob_quota_bytes:
        return _sealed(StorageSealReason.WORKSPACE_QUOTA)
    remaining_filesystem_bytes = (
        state.available_filesystem_bytes - incoming_bytes
    )
    if remaining_filesystem_bytes < policy.disk_reserve_bytes:
        return _sealed(StorageSealReason.DISK_RESERVE)
    return WriteAdmission(allowed=True, seal_required=False)


def plan_garbage_collection(
    policy: RetentionPolicy,
    snapshot: SyncedSnapshot,
    blobs: tuple[RetainedBlob, ...],
    references: tuple[ArtifactReference, ...],
    legal_holds: tuple[ArtifactLegalHold, ...],
    *,
    planned_at: datetime,
) -> GarbageCollectionPlan:
    _validate_evidence_bounds(blobs, references, legal_holds)
    validated_planned_at = _UTC_TIMESTAMP_ADAPTER.validate_python(
        planned_at,
        strict=True,
    )
    if snapshot.synced_at > validated_planned_at:
        raise ValueError("snapshot cannot be synced after garbage collection planning")

    reachable = set(snapshot.reachable_content_sha256s)
    for reference in references:
        if reference.workspace_id != snapshot.workspace_id:
            continue
        if reference.created_at > validated_planned_at:
            raise ValueError("artifact reference cannot be created in the future")
        reachable.add(reference.content_sha256)

    held = {
        hold.content_sha256
        for hold in legal_holds
        if _hold_is_active(
            hold,
            workspace_id=snapshot.workspace_id,
            planned_at=validated_planned_at,
        )
    }
    eligible: list[RetainedBlob] = []
    observed_hashes: set[str] = set()
    for blob in blobs:
        if blob.workspace_id != snapshot.workspace_id:
            continue
        if blob.content_sha256 in observed_hashes:
            raise ValueError("retained blob evidence contains duplicate hashes")
        observed_hashes.add(blob.content_sha256)
        if blob.created_at > validated_planned_at:
            raise ValueError("retained blob cannot be created in the future")
        if blob.content_sha256 in reachable or blob.content_sha256 in held:
            continue
        if blob.tombstone is None:
            continue
        _validate_tombstone_policy(policy, blob)
        if blob.tombstone.tombstoned_at > validated_planned_at:
            raise ValueError("artifact cannot be tombstoned in the future")
        if blob.tombstone.delete_after > validated_planned_at:
            continue
        eligible.append(blob)
    eligible.sort(
        key=lambda blob: (
            (
                blob.tombstone.delete_after
                if blob.tombstone is not None
                else validated_planned_at
            ),
            blob.content_sha256,
        )
    )

    candidates: list[str] = []
    reclaimable_bytes = 0
    complete = True
    for blob in eligible:
        next_reclaimable_bytes = reclaimable_bytes + blob.size_bytes
        if (
            len(candidates) >= policy.maximum_gc_candidates
            or next_reclaimable_bytes > policy.maximum_gc_bytes
        ):
            complete = False
            break
        candidates.append(blob.content_sha256)
        reclaimable_bytes = next_reclaimable_bytes
    return GarbageCollectionPlan(
        workspace_id=snapshot.workspace_id,
        snapshot_sha256=snapshot.snapshot_sha256,
        candidate_content_sha256s=tuple(candidates),
        reclaimable_bytes=reclaimable_bytes,
        complete=complete,
        planned_at=validated_planned_at,
    )


def _sealed(reason: StorageSealReason) -> WriteAdmission:
    return WriteAdmission(
        allowed=False,
        seal_required=True,
        reason=reason,
    )


def _validate_evidence_bounds(
    blobs: tuple[RetainedBlob, ...],
    references: tuple[ArtifactReference, ...],
    legal_holds: tuple[ArtifactLegalHold, ...],
) -> None:
    evidence_groups = {
        "retained blobs": len(blobs),
        "artifact references": len(references),
        "artifact legal holds": len(legal_holds),
    }
    for evidence_name, evidence_count in evidence_groups.items():
        if evidence_count > MAXIMUM_TRACKED_BLOBS:
            raise ValueError(
                f"{evidence_name} exceed the {MAXIMUM_TRACKED_BLOBS}-record limit"
            )


def _hold_is_active(
    hold: ArtifactLegalHold,
    *,
    workspace_id: str,
    planned_at: datetime,
) -> bool:
    if hold.workspace_id != workspace_id:
        return False
    if hold.placed_at > planned_at:
        raise ValueError("artifact legal hold cannot be placed in the future")
    if hold.released_at is not None and hold.released_at > planned_at:
        raise ValueError("artifact legal hold cannot be released in the future")
    return hold.active


def _validate_tombstone_policy(
    policy: RetentionPolicy,
    blob: RetainedBlob,
) -> None:
    tombstone = blob.tombstone
    if tombstone is None:
        return
    retention_deadline = blob.created_at + timedelta(days=policy.retention_days)
    if tombstone.tombstoned_at < retention_deadline:
        raise ValueError("artifact was tombstoned before its retention period elapsed")
    grace_deadline = tombstone.tombstoned_at + timedelta(
        seconds=policy.garbage_collection_grace_seconds
    )
    if tombstone.delete_after < grace_deadline:
        raise ValueError(
            "artifact tombstone does not satisfy the collection grace period"
        )
