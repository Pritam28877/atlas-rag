"""Strict contracts for content-addressed artifact blob storage."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    Sha256,
    StrictProtocolModel,
    WorkspaceId,
)

MAXIMUM_BLOB_BYTES = 4 * 1024 * 1024 * 1024
MAXIMUM_RANGE_BYTES = 16 * 1024 * 1024
MAXIMUM_IO_CHUNK_BYTES = 4 * 1024 * 1024


class BlobErrorCode(StrEnum):
    CAPACITY = "capacity"
    CLOSED = "closed"
    HASH_MISMATCH = "hash_mismatch"
    INVALID_CHUNK = "invalid_chunk"
    INVALID_STORAGE = "invalid_storage"
    NOT_FOUND = "not_found"
    RANGE_NOT_SATISFIABLE = "range_not_satisfiable"
    SIZE_MISMATCH = "size_mismatch"
    STREAM_FAILED = "stream_failed"
    UNSUPPORTED_PLATFORM = "unsupported_platform"


class BlobStoreError(RuntimeError):
    """Stable blob failure that never exposes content or filesystem paths."""

    def __init__(self, code: BlobErrorCode) -> None:
        super().__init__("artifact blob operation failed")
        self.code = code


class BlobWriteRequest(StrictProtocolModel):
    expected_content_sha256: Sha256
    expected_size_bytes: int = Field(ge=1, le=MAXIMUM_BLOB_BYTES)


class BlobMetadata(StrictProtocolModel):
    workspace_id: WorkspaceId
    content_sha256: Sha256
    storage_reference_sha256: Sha256
    size_bytes: int = Field(ge=1, le=MAXIMUM_BLOB_BYTES)


class BlobWriteResult(StrictProtocolModel):
    metadata: BlobMetadata
    created: bool


class BlobRangeRequest(StrictProtocolModel):
    content_sha256: Sha256
    offset_bytes: int = Field(default=0, ge=0, lt=MAXIMUM_BLOB_BYTES)
    length_bytes: int = Field(default=1024 * 1024, ge=1, le=MAXIMUM_RANGE_BYTES)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.offset_bytes + self.length_bytes > MAXIMUM_BLOB_BYTES:
            raise ValueError("blob byte range exceeds maximum blob size")
        return self
