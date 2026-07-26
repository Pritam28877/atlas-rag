import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.artifacts import (
    AdmissionControlledBlobWriter,
    ArtifactTombstone,
    BlobErrorCode,
    BlobStoreError,
    BlobWriteRequest,
    LocalBlobStore,
    LocalGarbageCollector,
    RetentionPolicy,
    SealedJournalSegment,
    SyncedSnapshot,
    SyncedSnapshotBundle,
    plan_garbage_collection,
)
from app.services.harness.journal import (
    GlobalJournalReadRequest,
    SQLiteEventJournal,
    SQLiteJournalIntegrityVerifier,
    SQLiteRetentionStore,
    SQLiteSnapshotStore,
    SQLiteStorageStore,
)
from scripts.probe_harness_sqlite_kill import NOW, append_request

WORKSPACE_ID = "wsp_" + "0" * 32
MEBIBYTE = 1024 * 1024


async def chunks(value: bytes) -> AsyncIterator[bytes]:
    yield value


def test_retention_deletion_and_restart_preserve_journal_invariants(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        os.chmod(tmp_path, 0o700)
        database_path = tmp_path / "journal.sqlite3"
        journal = await SQLiteEventJournal.open(database_path, clock=lambda: NOW)
        append_result = await journal.append(append_request())
        await journal.close()

        policy = RetentionPolicy(
            blob_quota_bytes=MEBIBYTE,
            disk_reserve_bytes=MEBIBYTE,
        )
        blob_store = await LocalBlobStore.open(tmp_path, WORKSPACE_ID)
        storage_store = await SQLiteStorageStore.open(database_path)
        writer = AdmissionControlledBlobWriter(
            WORKSPACE_ID,
            policy,
            blob_store,
            storage_store,
            clock=lambda: NOW,
        )
        value = b"retention-restore-drill"
        content_sha256 = hashlib.sha256(value).hexdigest()
        await writer.put(
            BlobWriteRequest(
                expected_content_sha256=content_sha256,
                expected_size_bytes=len(value),
            ),
            chunks(value),
        )

        planned_at = NOW + timedelta(days=92)
        snapshot = SyncedSnapshot(
            workspace_id=WORKSPACE_ID,
            snapshot_sha256="a" * 64,
            manifest_sha256="b" * 64,
            through_journal_sequence=1,
            reachable_content_sha256s=(),
            synced_at=planned_at,
        )
        segment = SealedJournalSegment(
            workspace_id=WORKSPACE_ID,
            segment_sha256="c" * 64,
            snapshot_sha256=snapshot.snapshot_sha256,
            first_journal_sequence=1,
            last_journal_sequence=1,
            event_count=1,
            sealed_at=planned_at - timedelta(seconds=1),
        )
        snapshot_bundle = SyncedSnapshotBundle(
            snapshot=snapshot,
            segments=(segment,),
        )
        snapshot_store = await SQLiteSnapshotStore.open(database_path)
        await snapshot_store.save(snapshot_bundle)
        await snapshot_store.close()

        retention_store = await SQLiteRetentionStore.open(database_path)
        tombstone = ArtifactTombstone(
            workspace_id=WORKSPACE_ID,
            content_sha256=content_sha256,
            tombstoned_at=NOW + timedelta(days=90),
            delete_after=NOW + timedelta(days=91),
            reason="Retention period elapsed.",
        )
        await retention_store.add_tombstone(tombstone)
        evidence = await retention_store.load(WORKSPACE_ID)
        plan = plan_garbage_collection(
            policy,
            snapshot,
            evidence.blobs,
            evidence.references,
            evidence.legal_holds,
            planned_at=planned_at,
        )
        collector = LocalGarbageCollector(
            WORKSPACE_ID,
            blob_store,
            retention_store,
        )
        assert await collector.collect(plan) == (content_sha256,)
        await retention_store.close()
        await storage_store.close()
        await blob_store.close()

        restored_journal = await SQLiteEventJournal.open(database_path)
        page = await restored_journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=WORKSPACE_ID,
                after_journal_sequence=0,
            )
        )
        await restored_journal.close()
        verifier = await SQLiteJournalIntegrityVerifier.open(database_path)
        verification = await verifier.verify()
        await verifier.close()
        restored_snapshots = await SQLiteSnapshotStore.open(database_path)
        restored_snapshot = await restored_snapshots.load_latest(WORKSPACE_ID)
        await restored_snapshots.close()
        restored_retention = await SQLiteRetentionStore.open(database_path)
        restored_evidence = await restored_retention.load(WORKSPACE_ID)
        await restored_retention.close()
        restored_storage = await SQLiteStorageStore.open(database_path)
        storage_state = await restored_storage.state(
            WORKSPACE_ID,
            available_filesystem_bytes=4 * MEBIBYTE,
        )
        await restored_storage.close()
        reopened_blobs = await LocalBlobStore.open(tmp_path, WORKSPACE_ID)
        with pytest.raises(BlobStoreError) as missing:
            await reopened_blobs.inspect(content_sha256)
        await reopened_blobs.close()

        assert tuple(journal_event.event.event_id for journal_event in page.events) == (
            append_request().events[0].event_id,
        )
        assert verification.complete
        assert verification.events_checked == 1
        assert verification.receipts_checked == 1
        assert restored_snapshot == snapshot_bundle
        assert restored_evidence.blobs[0].garbage_collected_at == planned_at
        assert storage_state.used_blob_bytes == 0
        assert append_result.journal_sequences == (1,)
        assert missing.value.code is BlobErrorCode.NOT_FOUND

    asyncio.run(scenario())
