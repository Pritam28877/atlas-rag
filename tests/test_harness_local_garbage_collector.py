import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.artifacts import (
    ArtifactTombstone,
    BlobErrorCode,
    BlobStoreError,
    BlobWriteRequest,
    LocalBlobStore,
    LocalGarbageCollector,
    RetainedBlob,
    RetentionPolicy,
    SyncedSnapshot,
    SyncedSnapshotBundle,
    plan_garbage_collection,
)
from app.services.harness.journal import (
    SQLiteRetentionStore,
    SQLiteSnapshotStore,
)

NOW = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "8" * 32


async def chunks(value: bytes) -> AsyncIterator[bytes]:
    yield value


def test_collection_retry_finishes_authorized_file_deletion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        os.chmod(tmp_path, 0o700)
        database_path = tmp_path / "journal.sqlite3"
        value = b"crash-retryable-retention"
        content_sha256 = hashlib.sha256(value).hexdigest()
        blob_store = await LocalBlobStore.open(tmp_path, WORKSPACE_ID)
        await blob_store.put(
            BlobWriteRequest(
                expected_content_sha256=content_sha256,
                expected_size_bytes=len(value),
            ),
            chunks(value),
        )
        snapshot = SyncedSnapshot(
            workspace_id=WORKSPACE_ID,
            snapshot_sha256="a" * 64,
            manifest_sha256="b" * 64,
            through_journal_sequence=0,
            reachable_content_sha256s=(),
            synced_at=NOW,
        )
        snapshot_store = await SQLiteSnapshotStore.open(database_path)
        await snapshot_store.save(
            SyncedSnapshotBundle(snapshot=snapshot, segments=())
        )
        await snapshot_store.close()

        retention_store = await SQLiteRetentionStore.open(database_path)
        retained_blob = RetainedBlob(
            workspace_id=WORKSPACE_ID,
            content_sha256=content_sha256,
            size_bytes=len(value),
            created_at=NOW - timedelta(days=200),
        )
        tombstone = ArtifactTombstone(
            workspace_id=WORKSPACE_ID,
            content_sha256=content_sha256,
            tombstoned_at=NOW - timedelta(days=2),
            delete_after=NOW - timedelta(days=1),
            reason="Retention period elapsed.",
        )
        await retention_store.record_blob(retained_blob)
        await retention_store.add_tombstone(tombstone)
        evidence = await retention_store.load(WORKSPACE_ID)
        plan = plan_garbage_collection(
            RetentionPolicy(disk_reserve_bytes=1024 * 1024),
            snapshot,
            evidence.blobs,
            evidence.references,
            evidence.legal_holds,
            planned_at=NOW,
        )

        def simulate_crash(_: str) -> None:
            raise RuntimeError("simulated crash after durable authorization")

        interrupted = LocalGarbageCollector(
            WORKSPACE_ID,
            blob_store,
            retention_store,
            after_authorization=simulate_crash,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            await interrupted.collect(plan)
        assert await blob_store.inspect(content_sha256)
        authorized = await retention_store.load(WORKSPACE_ID)
        assert authorized.blobs[0].garbage_collected_at == NOW

        collector = LocalGarbageCollector(
            WORKSPACE_ID,
            blob_store,
            retention_store,
        )
        assert await collector.collect(plan) == (content_sha256,)
        assert await collector.collect(plan) == (content_sha256,)
        with pytest.raises(BlobStoreError) as missing:
            await blob_store.inspect(content_sha256)
        await retention_store.close()
        await blob_store.close()

        assert missing.value.code is BlobErrorCode.NOT_FOUND

    asyncio.run(scenario())
