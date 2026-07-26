import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.harness.artifacts import (
    AdmissionControlledBlobWriter,
    BlobErrorCode,
    BlobStoreError,
    BlobWriteRequest,
    LocalBlobStore,
    RetentionPolicy,
    StorageAdmissionError,
    StorageSealReason,
)
from app.services.harness.journal import SQLiteStorageStore
from scripts.probe_harness_sqlite_kill import NOW

WORKSPACE_ID = "wsp_" + "0" * 32
MEBIBYTE = 1024 * 1024


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def request(value: bytes) -> BlobWriteRequest:
    return BlobWriteRequest(
        expected_content_sha256=hashlib.sha256(value).hexdigest(),
        expected_size_bytes=len(value),
    )


def prepare_root(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path


def test_writer_measures_real_capacity_and_commits_usage(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = prepare_root(tmp_path)
        blob_store = await LocalBlobStore.open(root, WORKSPACE_ID)
        storage_store = await SQLiteStorageStore.open(root / "journal.sqlite3")
        available = await blob_store.available_bytes()
        writer = AdmissionControlledBlobWriter(
            WORKSPACE_ID,
            RetentionPolicy(
                blob_quota_bytes=MEBIBYTE,
                disk_reserve_bytes=MEBIBYTE,
            ),
            blob_store,
            storage_store,
            clock=lambda: NOW,
        )
        value = b"admission-controlled"

        first = await writer.put(request(value), chunks(value[:5], value[5:]))
        duplicate = await writer.put(request(value), chunks(value))
        state = await storage_store.state(
            WORKSPACE_ID,
            available_filesystem_bytes=available,
        )
        await storage_store.close()
        await blob_store.close()

        assert available > MEBIBYTE
        assert first.created
        assert not duplicate.created
        assert state.used_blob_bytes == len(value)
        assert state.reserved_blob_bytes == 0

    asyncio.run(scenario())


def test_denied_write_seals_before_creating_temporary_file(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = prepare_root(tmp_path)
        blob_store = await LocalBlobStore.open(root, WORKSPACE_ID)
        storage_store = await SQLiteStorageStore.open(root / "journal.sqlite3")
        writer = AdmissionControlledBlobWriter(
            WORKSPACE_ID,
            RetentionPolicy(
                blob_quota_bytes=MEBIBYTE,
                disk_reserve_bytes=MEBIBYTE,
            ),
            blob_store,
            storage_store,
            capacity_reader=lambda: asyncio.sleep(
                0,
                result=MEBIBYTE,
            ),
            clock=lambda: NOW,
        )
        value = b"blocked-before-stream"

        with pytest.raises(StorageAdmissionError) as denied:
            await writer.put(request(value), chunks(value))
        temporary_files = tuple(
            (root / WORKSPACE_ID / "temporary").iterdir()
        )
        state = await storage_store.state(
            WORKSPACE_ID,
            available_filesystem_bytes=MEBIBYTE,
        )
        await storage_store.close()
        await blob_store.close()

        assert denied.value.reason is StorageSealReason.DISK_RESERVE
        assert state.sealed
        assert temporary_files == ()

    asyncio.run(scenario())


def test_failed_publish_releases_exact_reservation(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = prepare_root(tmp_path)
        blob_store = await LocalBlobStore.open(root, WORKSPACE_ID)
        storage_store = await SQLiteStorageStore.open(root / "journal.sqlite3")
        writer = AdmissionControlledBlobWriter(
            WORKSPACE_ID,
            RetentionPolicy(
                blob_quota_bytes=MEBIBYTE,
                disk_reserve_bytes=MEBIBYTE,
            ),
            blob_store,
            storage_store,
            capacity_reader=lambda: asyncio.sleep(
                0,
                result=4 * MEBIBYTE,
            ),
            clock=lambda: NOW,
        )
        invalid = BlobWriteRequest(
            expected_content_sha256="0" * 64,
            expected_size_bytes=6,
        )

        with pytest.raises(BlobStoreError) as failure:
            await writer.put(invalid, chunks(b"actual"))
        state = await storage_store.state(
            WORKSPACE_ID,
            available_filesystem_bytes=4 * MEBIBYTE,
        )
        temporary_files = tuple(
            (root / WORKSPACE_ID / "temporary").iterdir()
        )
        await storage_store.close()
        await blob_store.close()

        assert failure.value.code is BlobErrorCode.HASH_MISMATCH
        assert state.used_blob_bytes == 0
        assert state.reserved_blob_bytes == 0
        assert temporary_files == ()

    asyncio.run(scenario())
