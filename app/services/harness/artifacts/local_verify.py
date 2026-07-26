"""Bounded integrity verification for an opened local blob directory."""

import hashlib
import hmac
import os
import stat

from app.services.harness.artifacts.contracts import (
    MAXIMUM_BLOB_BYTES,
    BlobErrorCode,
    BlobStoreError,
)

READ_CHUNK_BYTES = 1024 * 1024


def verify_blob(
    shard_descriptor: int,
    content_sha256: str,
    expected_size: int | None,
) -> int:
    file_descriptor, size_bytes = open_verified_blob(
        shard_descriptor,
        content_sha256,
    )
    os.close(file_descriptor)
    if expected_size is not None and size_bytes != expected_size:
        raise BlobStoreError(BlobErrorCode.SIZE_MISMATCH)
    return size_bytes


def open_verified_blob(
    shard_descriptor: int,
    content_sha256: str,
) -> tuple[int, int]:
    try:
        file_descriptor = os.open(
            content_sha256,
            os.O_RDONLY | _no_follow_flag(),
            dir_fd=shard_descriptor,
        )
    except FileNotFoundError as error:
        raise BlobStoreError(BlobErrorCode.NOT_FOUND) from error
    try:
        status = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or stat.S_IMODE(status.st_mode) != 0o400
            or status.st_nlink != 1
            or not 1 <= status.st_size <= MAXIMUM_BLOB_BYTES
        ):
            raise BlobStoreError(BlobErrorCode.INVALID_STORAGE)
        digest = hashlib.sha256()
        bytes_read = 0
        while value := os.read(file_descriptor, READ_CHUNK_BYTES):
            digest.update(value)
            bytes_read += len(value)
        if bytes_read != status.st_size:
            raise BlobStoreError(BlobErrorCode.INVALID_STORAGE)
        if not hmac.compare_digest(digest.hexdigest(), content_sha256):
            raise BlobStoreError(BlobErrorCode.HASH_MISMATCH)
        return file_descriptor, status.st_size
    except BaseException:
        os.close(file_descriptor)
        raise


def _no_follow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))
