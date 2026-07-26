import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.artifacts import (
    BlobErrorCode,
    BlobRangeRequest,
    BlobStoreError,
    BlobWriteRequest,
    LocalBlobStore,
)

WORKSPACE_ID = "wsp_" + "2" * 32


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def private_root(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path


def test_roots_identifiers_and_digests_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = private_root(tmp_path)
        with pytest.raises(ValidationError):
            await LocalBlobStore.open(root, "../workspace")

        os.chmod(root, 0o755)
        with pytest.raises(BlobStoreError) as public_root:
            await LocalBlobStore.open(root, WORKSPACE_ID)
        assert public_root.value.code is BlobErrorCode.INVALID_STORAGE

        os.chmod(root, 0o700)
        linked_root = root.parent / f"{root.name}-link"
        linked_root.symlink_to(root, target_is_directory=True)
        try:
            with pytest.raises(BlobStoreError) as symlink_root:
                await LocalBlobStore.open(linked_root, WORKSPACE_ID)
            assert symlink_root.value.code is BlobErrorCode.INVALID_STORAGE
        finally:
            linked_root.unlink()

    asyncio.run(scenario())

    with pytest.raises(ValidationError):
        BlobWriteRequest(
            expected_content_sha256="../../private",
            expected_size_bytes=1,
        )
    with pytest.raises(ValidationError):
        BlobRangeRequest(
            content_sha256="0" * 64,
            offset_bytes=4 * 1024 * 1024 * 1024 - 1,
            length_bytes=2,
        )


def test_destination_symlink_is_never_followed(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = private_root(tmp_path)
        value = b"symlink-resistant-content"
        digest = hashlib.sha256(value).hexdigest()
        outside = root.parent / f"{root.name}-outside"
        outside.write_bytes(b"outside")
        shard = root / WORKSPACE_ID / "blobs" / digest[:2]
        store = await LocalBlobStore.open(root, WORKSPACE_ID)
        shard.mkdir(mode=0o700)
        (shard / digest).symlink_to(outside)
        try:
            with pytest.raises(BlobStoreError) as failure:
                await store.put(
                    BlobWriteRequest(
                        expected_content_sha256=digest,
                        expected_size_bytes=len(value),
                    ),
                    chunks(value),
                )
            assert failure.value.code is BlobErrorCode.INVALID_STORAGE
            assert outside.read_bytes() == b"outside"
        finally:
            await store.close()
            outside.unlink()

    asyncio.run(scenario())


def test_shard_directory_symlink_is_never_followed(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = private_root(tmp_path)
        value = b"shard-symlink-resistant"
        digest = hashlib.sha256(value).hexdigest()
        outside = root.parent / f"{root.name}-outside-directory"
        outside.mkdir(mode=0o700)
        store = await LocalBlobStore.open(root, WORKSPACE_ID)
        shard_link = root / WORKSPACE_ID / "blobs" / digest[:2]
        shard_link.symlink_to(outside, target_is_directory=True)
        try:
            with pytest.raises(BlobStoreError) as failure:
                await store.put(
                    BlobWriteRequest(
                        expected_content_sha256=digest,
                        expected_size_bytes=len(value),
                    ),
                    chunks(value),
                )
            assert failure.value.code is BlobErrorCode.INVALID_STORAGE
            assert tuple(outside.iterdir()) == ()
        finally:
            await store.close()
            shard_link.unlink()
            outside.rmdir()

    asyncio.run(scenario())


def test_corruption_is_detected_before_range_bytes_are_returned(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = private_root(tmp_path)
        original = b"original-content"
        digest = hashlib.sha256(original).hexdigest()
        store = await LocalBlobStore.open(root, WORKSPACE_ID)
        await store.put(
            BlobWriteRequest(
                expected_content_sha256=digest,
                expected_size_bytes=len(original),
            ),
            chunks(original),
        )
        blob_path = root / WORKSPACE_ID / "blobs" / digest[:2] / digest
        os.chmod(blob_path, 0o600)
        blob_path.write_bytes(b"corrupted-bytes")
        os.chmod(blob_path, 0o400)

        with pytest.raises(BlobStoreError) as failure:
            async for _ in store.read_range(
                BlobRangeRequest(
                    content_sha256=digest,
                    offset_bytes=0,
                    length_bytes=4,
                )
            ):
                pytest.fail("corrupt content was returned")
        await store.close()
        assert failure.value.code is BlobErrorCode.HASH_MISMATCH

    asyncio.run(scenario())


def test_empty_and_oversized_chunks_are_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = private_root(tmp_path)
        store = await LocalBlobStore.open(root, WORKSPACE_ID)
        empty_request = BlobWriteRequest(
            expected_content_sha256=hashlib.sha256(b"x").hexdigest(),
            expected_size_bytes=1,
        )
        with pytest.raises(BlobStoreError) as empty:
            await store.put(empty_request, chunks(b""))

        oversized_value = b"x" * (4 * 1024 * 1024 + 1)
        oversized_request = BlobWriteRequest(
            expected_content_sha256=hashlib.sha256(
                oversized_value
            ).hexdigest(),
            expected_size_bytes=len(oversized_value),
        )
        with pytest.raises(BlobStoreError) as oversized:
            await store.put(oversized_request, chunks(oversized_value))
        await store.close()

        assert empty.value.code is BlobErrorCode.INVALID_CHUNK
        assert oversized.value.code is BlobErrorCode.INVALID_CHUNK

    asyncio.run(scenario())
