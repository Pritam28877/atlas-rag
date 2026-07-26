from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.artifacts import (
    ArtifactLegalHold,
    ArtifactReference,
    ArtifactTombstone,
    RetainedBlob,
    RetentionPolicy,
    StorageSealReason,
    SyncedSnapshot,
    WorkspaceStorageState,
    admit_blob_write,
    plan_garbage_collection,
)
from app.services.harness.protocol.retention import (
    MAXIMUM_TRACKED_BLOBS,
)

NOW = datetime(2026, 7, 27, 6, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32


def digest(number: int) -> str:
    return f"{number:064x}"


def tombstone(number: int, *, eligible: bool = True) -> ArtifactTombstone:
    tombstoned_at = NOW - timedelta(days=2)
    delete_after = (
        NOW - timedelta(days=1)
        if eligible
        else NOW + timedelta(days=1)
    )
    return ArtifactTombstone(
        workspace_id=WORKSPACE_ID,
        content_sha256=digest(number),
        tombstoned_at=tombstoned_at,
        delete_after=delete_after,
        reason="Retention policy expired this artifact.",
    )


def blob(
    number: int,
    *,
    size_bytes: int = 10,
    marked: ArtifactTombstone | None = None,
) -> RetainedBlob:
    return RetainedBlob(
        workspace_id=WORKSPACE_ID,
        content_sha256=digest(number),
        size_bytes=size_bytes,
        created_at=NOW - timedelta(days=200),
        tombstone=marked,
    )


def snapshot(*reachable: str) -> SyncedSnapshot:
    return SyncedSnapshot(
        workspace_id=WORKSPACE_ID,
        snapshot_sha256=digest(100),
        manifest_sha256=digest(101),
        through_journal_sequence=50,
        reachable_content_sha256s=tuple(sorted(reachable)),
        synced_at=NOW,
    )


def test_gc_requires_tombstone_grace_and_excludes_reachable_and_held() -> None:
    policy = RetentionPolicy(
        disk_reserve_bytes=1024 * 1024,
        maximum_gc_candidates=10,
        maximum_gc_bytes=1024 * 1024,
    )
    records = (
        blob(1, marked=tombstone(1)),
        blob(2, marked=tombstone(2)),
        blob(3, marked=tombstone(3)),
        blob(4, marked=tombstone(4, eligible=False)),
        blob(5),
    )
    holds = (
        ArtifactLegalHold(
            workspace_id=WORKSPACE_ID,
            hold_sha256=digest(200),
            content_sha256=digest(2),
            placed_at=NOW - timedelta(days=3),
        ),
    )

    plan = plan_garbage_collection(
        policy,
        snapshot(digest(1)),
        records,
        (),
        holds,
        planned_at=NOW,
    )

    assert plan.candidate_content_sha256s == (digest(3),)
    assert plan.reclaimable_bytes == 10
    assert plan.complete


def test_gc_is_deterministic_and_bounded_by_count_and_bytes() -> None:
    records = tuple(
        blob(
            number,
            size_bytes=400 * 1024,
            marked=tombstone(number),
        )
        for number in (3, 1, 4, 2)
    )
    count_limited = plan_garbage_collection(
        RetentionPolicy(
            disk_reserve_bytes=1024 * 1024,
            maximum_gc_candidates=2,
            maximum_gc_bytes=1024 * 1024,
        ),
        snapshot(),
        records,
        (),
        (),
        planned_at=NOW,
    )
    byte_limited = plan_garbage_collection(
        RetentionPolicy(
            disk_reserve_bytes=1024 * 1024,
            maximum_gc_candidates=10,
            maximum_gc_bytes=1024 * 1024,
        ),
        snapshot(),
        records,
        (),
        (),
        planned_at=NOW,
    )

    assert count_limited.candidate_content_sha256s == (digest(1), digest(2))
    assert count_limited.reclaimable_bytes == 800 * 1024
    assert not count_limited.complete
    assert byte_limited.candidate_content_sha256s == (digest(1), digest(2))
    assert byte_limited.reclaimable_bytes == 800 * 1024
    assert not byte_limited.complete


def test_gc_excludes_references_created_after_the_synced_snapshot() -> None:
    referenced_blob = blob(6, marked=tombstone(6))
    reference = ArtifactReference(
        workspace_id=WORKSPACE_ID,
        reference_sha256=digest(206),
        content_sha256=referenced_blob.content_sha256,
        created_at=NOW,
    )

    plan = plan_garbage_collection(
        RetentionPolicy(disk_reserve_bytes=1024 * 1024),
        snapshot(),
        (referenced_blob,),
        (reference,),
        (),
        planned_at=NOW,
    )

    assert plan.candidate_content_sha256s == ()
    assert plan.reclaimable_bytes == 0
    assert plan.complete


def test_write_admission_seals_at_quota_and_reserve_boundaries() -> None:
    policy = RetentionPolicy(
        blob_quota_bytes=2 * 1024 * 1024,
        disk_reserve_bytes=1024 * 1024,
    )
    state = WorkspaceStorageState(
        workspace_id=WORKSPACE_ID,
        used_blob_bytes=1024 * 1024,
        available_filesystem_bytes=2 * 1024 * 1024,
    )

    allowed = admit_blob_write(policy, state, 1024 * 1024)
    quota = admit_blob_write(policy, state, 1024 * 1024 + 1)
    reserve = admit_blob_write(
        policy,
        state.model_copy(
            update={"available_filesystem_bytes": 2 * 1024 * 1024 - 1}
        ),
        1024 * 1024,
    )
    sealed = admit_blob_write(
        policy,
        state.model_copy(update={"sealed": True}),
        1,
    )

    assert allowed.allowed
    assert quota.reason is StorageSealReason.WORKSPACE_QUOTA
    assert reserve.reason is StorageSealReason.DISK_RESERVE
    assert sealed.reason is StorageSealReason.ALREADY_SEALED


def test_retention_evidence_rejects_ambiguous_scope_and_order() -> None:
    with pytest.raises(ValidationError, match="unique and sorted"):
        SyncedSnapshot(
            workspace_id=WORKSPACE_ID,
            snapshot_sha256=digest(100),
            manifest_sha256=digest(101),
            through_journal_sequence=50,
            reachable_content_sha256s=(digest(2), digest(1)),
            synced_at=NOW,
        )
    with pytest.raises(ValidationError, match="future delete"):
        ArtifactTombstone(
            workspace_id=WORKSPACE_ID,
            content_sha256=digest(1),
            tombstoned_at=NOW,
            delete_after=NOW,
            reason="Invalid grace.",
        )
    with pytest.raises(ValidationError, match="scope"):
        blob(
            1,
            marked=tombstone(2),
        )
    with pytest.raises(ValidationError, match="release"):
        ArtifactLegalHold(
            workspace_id=WORKSPACE_ID,
            hold_sha256=digest(201),
            content_sha256=digest(1),
            placed_at=NOW,
            released_at=NOW,
        )


def test_gc_rejects_stale_or_ambiguous_timing_evidence() -> None:
    policy = RetentionPolicy(disk_reserve_bytes=1024 * 1024)
    future_reference = ArtifactReference(
        workspace_id=WORKSPACE_ID,
        reference_sha256=digest(207),
        content_sha256=digest(7),
        created_at=NOW + timedelta(seconds=1),
    )
    premature_tombstone = ArtifactTombstone(
        workspace_id=WORKSPACE_ID,
        content_sha256=digest(8),
        tombstoned_at=NOW - timedelta(days=150),
        delete_after=NOW - timedelta(days=149),
        reason="Premature policy expiry.",
    )

    with pytest.raises(ValueError, match="created in the future"):
        plan_garbage_collection(
            policy,
            snapshot(),
            (),
            (future_reference,),
            (),
            planned_at=NOW,
        )
    with pytest.raises(ValueError, match="retention period"):
        plan_garbage_collection(
            policy,
            snapshot(),
            (blob(8, marked=premature_tombstone),),
            (),
            (),
            planned_at=NOW,
        )
    with pytest.raises(ValidationError, match="explicit UTC"):
        plan_garbage_collection(
            policy,
            snapshot(),
            (),
            (),
            (),
            planned_at=NOW.replace(tzinfo=None),
        )


def test_gc_rejects_unbounded_evidence_before_allocating_work_sets() -> None:
    repeated_blob = blob(9)

    with pytest.raises(ValueError, match="retained blobs.*record limit"):
        plan_garbage_collection(
            RetentionPolicy(disk_reserve_bytes=1024 * 1024),
            snapshot(),
            (repeated_blob,) * (MAXIMUM_TRACKED_BLOBS + 1),
            (),
            (),
            planned_at=NOW,
        )
