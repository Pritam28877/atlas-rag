import asyncio
import os
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.artifacts import (
    ArtifactLegalHold,
    ArtifactReference,
    ArtifactTombstone,
    RetainedBlob,
)
from app.services.harness.journal import (
    RetentionStoreConflict,
    SQLiteRetentionStore,
)
from scripts.probe_harness_sqlite_kill import NOW

WORKSPACE_ID = "wsp_" + "0" * 32


def digest(number: int) -> str:
    return f"{number:064x}"


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def blob() -> RetainedBlob:
    return RetainedBlob(
        workspace_id=WORKSPACE_ID,
        content_sha256=digest(1),
        size_bytes=4096,
        created_at=NOW,
    )


def reference() -> ArtifactReference:
    return ArtifactReference(
        workspace_id=WORKSPACE_ID,
        reference_sha256=digest(2),
        content_sha256=digest(1),
        created_at=NOW + timedelta(seconds=1),
    )


def tombstone() -> ArtifactTombstone:
    return ArtifactTombstone(
        workspace_id=WORKSPACE_ID,
        content_sha256=digest(1),
        tombstoned_at=NOW + timedelta(days=100),
        delete_after=NOW + timedelta(days=101),
        reason="Workspace retention expired.",
    )


def hold() -> ArtifactLegalHold:
    return ArtifactLegalHold(
        workspace_id=WORKSPACE_ID,
        hold_sha256=digest(3),
        content_sha256=digest(1),
        placed_at=NOW + timedelta(seconds=2),
    )


def test_retention_evidence_restores_after_release_and_reopen(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        store = await SQLiteRetentionStore.open(path)
        await store.record_blob(blob())
        await store.add_reference(reference())
        await store.add_tombstone(tombstone())
        await store.place_hold(hold())
        released_at = NOW + timedelta(days=1)
        released_reference = await store.release_reference(
            WORKSPACE_ID,
            reference().reference_sha256,
            released_at=released_at,
        )
        released_hold = await store.release_hold(
            WORKSPACE_ID,
            hold().hold_sha256,
            released_at=released_at,
        )
        await store.close()

        reopened = await SQLiteRetentionStore.open(path)
        restored = await reopened.load(WORKSPACE_ID)
        await reopened.close()

        assert not released_reference.active
        assert not released_hold.active
        assert restored.references == (released_reference,)
        assert restored.legal_holds == (released_hold,)
        assert restored.blobs[0].tombstone == tombstone()

    asyncio.run(scenario())


def test_retention_writes_are_idempotent_and_conflicts_are_explicit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        store = await SQLiteRetentionStore.open(path)

        assert await store.record_blob(blob()) == blob()
        assert await store.record_blob(blob()) == blob()
        assert await store.add_reference(reference()) == reference()
        assert await store.add_reference(reference()) == reference()
        assert await store.add_tombstone(tombstone()) == tombstone()
        assert await store.add_tombstone(tombstone()) == tombstone()
        assert await store.place_hold(hold()) == hold()
        assert await store.place_hold(hold()) == hold()

        conflicting_blob = blob().model_copy(update={"size_bytes": 8192})
        with pytest.raises(RetentionStoreConflict, match="different evidence"):
            await store.record_blob(conflicting_blob)
        with pytest.raises(RetentionStoreConflict, match="durable evidence"):
            await store.add_tombstone(
                tombstone().model_copy(
                    update={"reason": "A conflicting retention reason."}
                )
            )
        await store.close()

    asyncio.run(scenario())


def test_retention_rejects_missing_blob_and_invalid_release(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        store = await SQLiteRetentionStore.open(path)

        with pytest.raises(RetentionStoreConflict):
            await store.add_reference(reference())
        await store.record_blob(blob())
        await store.add_reference(reference())
        with pytest.raises(RetentionStoreConflict, match="invalid"):
            await store.release_reference(
                WORKSPACE_ID,
                reference().reference_sha256,
                released_at=NOW,
            )
        with pytest.raises(ValueError, match="UTC"):
            await store.release_reference(
                WORKSPACE_ID,
                reference().reference_sha256,
                released_at=NOW.replace(tzinfo=None),
            )
        await store.close()

    asyncio.run(scenario())
