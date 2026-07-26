import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.harness.artifacts import (
    BlobErrorCode,
    BlobRangeRequest,
    BlobStoreError,
    BlobWriteRequest,
    LocalBlobStore,
)

WORKSPACE_ID = "wsp_" + "1" * 32


def storage_root(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


async def collect(values: AsyncIterator[bytes]) -> bytes:
    collected = bytearray()
    async for value in values:
        collected.extend(value)
    return bytes(collected)


def write_request(value: bytes, *, size: int | None = None) -> BlobWriteRequest:
    return BlobWriteRequest(
        expected_content_sha256=hashlib.sha256(value).hexdigest(),
        expected_size_bytes=len(value) if size is None else size,
    )


def test_stream_publish_duplicate_and_bounded_ranges(tmp_path: Path) -> None:
    async def scenario() -> None:
        value = b"content-addressed-blob"
        request = write_request(value)
        store = await LocalBlobStore.open(
            storage_root(tmp_path),
            WORKSPACE_ID,
            read_chunk_bytes=3,
        )
        first = await store.put(
            request,
            chunks(value[:4], value[4:12], value[12:]),
        )
        duplicate = await store.put(request, chunks(value))
        metadata = await store.inspect(request.expected_content_sha256)
        middle = await collect(
            store.read_range(
                BlobRangeRequest(
                    content_sha256=request.expected_content_sha256,
                    offset_bytes=2,
                    length_bytes=7,
                )
            )
        )
        tail = await collect(
            store.read_range(
                BlobRangeRequest(
                    content_sha256=request.expected_content_sha256,
                    offset_bytes=len(value) - 3,
                    length_bytes=10,
                )
            )
        )
        with pytest.raises(BlobStoreError) as invalid_range:
            await collect(
                store.read_range(
                    BlobRangeRequest(
                        content_sha256=request.expected_content_sha256,
                        offset_bytes=len(value),
                        length_bytes=1,
                    )
                )
            )
        blob_path = (
            tmp_path
            / WORKSPACE_ID
            / "blobs"
            / request.expected_content_sha256[:2]
            / request.expected_content_sha256
        )
        directory_modes = tuple(
            path.stat().st_mode & 0o777
            for path in (
                tmp_path / WORKSPACE_ID,
                tmp_path / WORKSPACE_ID / "blobs",
                tmp_path / WORKSPACE_ID / "temporary",
            )
        )
        blob_mode = blob_path.stat().st_mode & 0o777
        await store.close()
        with pytest.raises(BlobStoreError) as closed:
            await store.inspect(request.expected_content_sha256)

        assert first.created
        assert not duplicate.created
        assert duplicate.metadata == first.metadata
        assert metadata == first.metadata
        assert middle == value[2:9]
        assert tail == value[-3:]
        assert invalid_range.value.code is BlobErrorCode.RANGE_NOT_SATISFIABLE
        assert first.metadata.storage_reference_sha256 != (
            first.metadata.content_sha256
        )
        assert directory_modes == (0o700, 0o700, 0o700)
        assert blob_mode == 0o400
        assert closed.value.code is BlobErrorCode.CLOSED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("stored_value", "blob_request", "expected_code"),
    (
        (
            b"actual",
            BlobWriteRequest(
                expected_content_sha256="0" * 64,
                expected_size_bytes=6,
            ),
            BlobErrorCode.HASH_MISMATCH,
        ),
        (
            b"short",
            write_request(b"short", size=6),
            BlobErrorCode.SIZE_MISMATCH,
        ),
        (
            b"too-long",
            write_request(b"too-long", size=3),
            BlobErrorCode.SIZE_MISMATCH,
        ),
    ),
)
def test_digest_and_size_mismatch_publish_nothing(
    tmp_path: Path,
    stored_value: bytes,
    blob_request: BlobWriteRequest,
    expected_code: BlobErrorCode,
) -> None:
    async def scenario() -> None:
        store = await LocalBlobStore.open(
            storage_root(tmp_path),
            WORKSPACE_ID,
        )
        with pytest.raises(BlobStoreError) as failure:
            await store.put(blob_request, chunks(stored_value))
        with pytest.raises(BlobStoreError) as missing:
            await store.inspect(blob_request.expected_content_sha256)
        temporary_files = tuple(
            (tmp_path / WORKSPACE_ID / "temporary").iterdir()
        )
        await store.close()

        assert failure.value.code is expected_code
        assert missing.value.code is BlobErrorCode.NOT_FOUND
        assert temporary_files == ()

    asyncio.run(scenario())


def test_partial_stream_failure_removes_private_temporary_file(
    tmp_path: Path,
) -> None:
    async def failing_chunks() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("private producer path")

    async def scenario() -> None:
        request = write_request(b"partial-and-more")
        store = await LocalBlobStore.open(
            storage_root(tmp_path),
            WORKSPACE_ID,
        )
        with pytest.raises(
            BlobStoreError,
            match="artifact blob operation failed",
        ) as failure:
            await store.put(request, failing_chunks())
        temporary_files = tuple(
            (tmp_path / WORKSPACE_ID / "temporary").iterdir()
        )
        await store.close()

        assert failure.value.code is BlobErrorCode.STREAM_FAILED
        assert "private producer path" not in str(failure.value)
        assert temporary_files == ()

    asyncio.run(scenario())


def test_concurrent_duplicate_publish_has_one_creator(tmp_path: Path) -> None:
    async def scenario() -> None:
        value = b"same-concurrent-content"
        request = write_request(value)
        store = await LocalBlobStore.open(
            storage_root(tmp_path),
            WORKSPACE_ID,
        )
        outcomes = await asyncio.gather(
            store.put(request, chunks(value[:5], value[5:])),
            store.put(request, chunks(value)),
        )
        restored = await collect(
            store.read_range(
                BlobRangeRequest(
                    content_sha256=request.expected_content_sha256,
                    offset_bytes=0,
                    length_bytes=len(value),
                )
            )
        )
        await store.close()

        assert sum(outcome.created for outcome in outcomes) == 1
        assert outcomes[0].metadata == outcomes[1].metadata
        assert restored == value

    asyncio.run(scenario())


def test_cancelled_stream_removes_partial_temporary_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        started_waiting = asyncio.Event()
        never_finish = asyncio.Event()

        async def blocked_chunks() -> AsyncIterator[bytes]:
            yield b"partial"
            started_waiting.set()
            await never_finish.wait()
            yield b"unreachable"

        store = await LocalBlobStore.open(
            storage_root(tmp_path),
            WORKSPACE_ID,
        )
        request = write_request(b"partial-unreachable")
        upload = asyncio.create_task(store.put(request, blocked_chunks()))
        await started_waiting.wait()
        upload.cancel()
        with pytest.raises(asyncio.CancelledError):
            await upload
        temporary_files = tuple(
            (tmp_path / WORKSPACE_ID / "temporary").iterdir()
        )
        await store.close()

        assert temporary_files == ()

    asyncio.run(scenario())


def test_large_blob_is_consumed_as_bounded_chunks(tmp_path: Path) -> None:
    async def scenario() -> None:
        chunk = b"x" * (1024 * 1024)
        chunk_count = 9
        digest = hashlib.sha256()
        for _ in range(chunk_count):
            digest.update(chunk)
        yielded_chunks = 0

        async def large_chunks() -> AsyncIterator[bytes]:
            nonlocal yielded_chunks
            for _ in range(chunk_count):
                yielded_chunks += 1
                yield chunk

        store = await LocalBlobStore.open(
            storage_root(tmp_path),
            WORKSPACE_ID,
        )
        result = await store.put(
            BlobWriteRequest(
                expected_content_sha256=digest.hexdigest(),
                expected_size_bytes=len(chunk) * chunk_count,
            ),
            large_chunks(),
        )
        restored = await store.inspect(result.metadata.content_sha256)
        await store.close()

        assert yielded_chunks == chunk_count
        assert result.created
        assert restored.size_bytes == len(chunk) * chunk_count

    asyncio.run(scenario())
