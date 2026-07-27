"""Content-addressed persistence for bounded background-job output."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Literal, Protocol

from app.services.harness.artifacts.contracts import BlobWriteRequest
from app.services.harness.protocol.background_jobs import (
    BackgroundJobArtifact,
    BackgroundJobRecord,
)
from app.services.harness.protocol.base import MediaType


class BackgroundBlobWriter(Protocol):
    async def put(
        self,
        request: BlobWriteRequest,
        chunks: AsyncIterator[bytes],
    ) -> object: ...


class LocalBackgroundJobArtifactWriter:
    def __init__(
        self,
        blob_writer: BackgroundBlobWriter,
    ) -> None:
        self._blob_writer = blob_writer

    async def write(
        self,
        job: BackgroundJobRecord,
        *,
        kind: Literal["log", "result"],
        media_type: MediaType,
        content: bytes,
    ) -> BackgroundJobArtifact:
        content_sha256 = hashlib.sha256(content).hexdigest()
        await self._blob_writer.put(
            BlobWriteRequest(
                expected_content_sha256=content_sha256,
                expected_size_bytes=len(content),
            ),
            _one_chunk(content),
        )
        identity = (
            f"atlas-background-v1:{job.workspace_id}:{job.operation_id}:"
            f"{kind}:{content_sha256}"
        )
        artifact_id = f"art_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
        return BackgroundJobArtifact(
            artifact_id=artifact_id,
            media_type=media_type,
            size_bytes=len(content),
            content_sha256=content_sha256,
        )


async def _one_chunk(content: bytes) -> AsyncIterator[bytes]:
    yield content
