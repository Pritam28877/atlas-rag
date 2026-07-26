"""Async content-addressed blob storage backed by private local files."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from types import TracebackType

from pydantic import TypeAdapter

from app.services.harness.artifacts.contracts import (
    BlobErrorCode,
    BlobMetadata,
    BlobRangeRequest,
    BlobStoreError,
    BlobWriteRequest,
    BlobWriteResult,
)
from app.services.harness.artifacts.local_files import (
    BlobRangeHandle,
    StagedBlob,
)
from app.services.harness.artifacts.local_io import LocalBlobIOOwner
from app.services.harness.protocol import WorkspaceId


class LocalBlobStore:
    """Workspace-bound local blob store with bounded off-event-loop I/O."""

    def __init__(
        self,
        workspace_id: WorkspaceId,
        io_owner: LocalBlobIOOwner,
        *,
        read_chunk_bytes: int,
    ) -> None:
        self._workspace_id = workspace_id
        self._io_owner = io_owner
        self._read_chunk_bytes = read_chunk_bytes

    @classmethod
    async def open(
        cls,
        storage_root: Path,
        workspace_id: str,
        *,
        read_chunk_bytes: int = 1024 * 1024,
        maximum_pending_operations: int = 64,
    ) -> LocalBlobStore:
        if not 1 <= read_chunk_bytes <= 1024 * 1024:
            raise ValueError(
                "blob read chunk bytes must be between 1 and 1048576"
            )
        validated_workspace_id: WorkspaceId = TypeAdapter(
            WorkspaceId
        ).validate_python(workspace_id, strict=True)
        io_owner = LocalBlobIOOwner(
            storage_root,
            validated_workspace_id,
            maximum_pending_operations=maximum_pending_operations,
        )
        await io_owner.initialize()
        return cls(
            validated_workspace_id,
            io_owner,
            read_chunk_bytes=read_chunk_bytes,
        )

    async def put(
        self,
        request: BlobWriteRequest,
        chunks: AsyncIterable[bytes],
    ) -> BlobWriteResult:
        staged = await self._io_owner.execute(self._io_owner.files.begin)
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise BlobStoreError(BlobErrorCode.INVALID_CHUNK)
                await self._io_owner.execute(
                    lambda: self._io_owner.files.write(
                        staged,
                        chunk,
                        request.expected_size_bytes,
                    )
                )
            created = await self._io_owner.execute(
                lambda: self._io_owner.files.publish(staged, request)
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._abort(staged))
            raise
        except BlobStoreError:
            await self._abort(staged)
            raise
        except Exception as error:
            await self._abort(staged)
            raise BlobStoreError(BlobErrorCode.STREAM_FAILED) from error
        return BlobWriteResult(
            metadata=self._metadata(
                request.expected_content_sha256,
                request.expected_size_bytes,
            ),
            created=created,
        )

    async def available_bytes(self) -> int:
        return await self._io_owner.execute(
            self._io_owner.files.available_bytes
        )

    async def inspect(self, content_sha256: str) -> BlobMetadata:
        request = BlobRangeRequest(
            content_sha256=content_sha256,
            offset_bytes=0,
            length_bytes=1,
        )
        size_bytes = await self._io_owner.execute(
            lambda: self._io_owner.files.inspect(request.content_sha256)
        )
        return self._metadata(request.content_sha256, size_bytes)

    async def delete(self, content_sha256: str) -> bool:
        request = BlobRangeRequest(
            content_sha256=content_sha256,
            offset_bytes=0,
            length_bytes=1,
        )
        return await self._io_owner.execute(
            lambda: self._io_owner.files.delete(request.content_sha256)
        )

    async def read_range(
        self,
        request: BlobRangeRequest,
    ) -> AsyncIterator[bytes]:
        handle = await self._io_owner.execute(
            lambda: self._io_owner.files.open_range(
                request.content_sha256,
                request.offset_bytes,
                request.length_bytes,
            )
        )
        try:
            while handle.remaining_bytes:
                value = await self._io_owner.execute(
                    lambda: self._io_owner.files.read_range(
                        handle,
                        self._read_chunk_bytes,
                    )
                )
                yield value
        finally:
            await self._close_range(handle)

    async def close(self) -> None:
        await self._io_owner.close()

    async def __aenter__(self) -> LocalBlobStore:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exception_type, exception, traceback
        await self.close()

    async def _abort(self, staged: StagedBlob) -> None:
        try:
            await self._io_owner.execute(
                lambda: self._io_owner.files.abort(staged)
            )
        except BlobStoreError:
            pass

    async def _close_range(self, handle: BlobRangeHandle) -> None:
        try:
            await self._io_owner.execute(
                lambda: self._io_owner.files.close_range(handle)
            )
        except BlobStoreError:
            pass

    def _metadata(
        self,
        content_sha256: str,
        size_bytes: int,
    ) -> BlobMetadata:
        storage_reference = (
            f"atlas-local-blob-v1:{self._workspace_id}:{content_sha256}"
        )
        return BlobMetadata(
            workspace_id=self._workspace_id,
            content_sha256=content_sha256,
            storage_reference_sha256=hashlib.sha256(
                storage_reference.encode()
            ).hexdigest(),
            size_bytes=size_bytes,
        )
