"""Descriptor-relative private filesystem operations for local blobs."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.harness.artifacts.contracts import (
    MAXIMUM_IO_CHUNK_BYTES,
    BlobErrorCode,
    BlobStoreError,
    BlobWriteRequest,
)
from app.services.harness.artifacts.local_verify import (
    open_verified_blob,
    verify_blob,
)


class HashState(Protocol):
    def update(self, value: bytes) -> None: ...
    def hexdigest(self) -> str: ...


@dataclass(slots=True)
class StagedBlob:
    name: str
    file_descriptor: int
    digest: HashState
    size_bytes: int = 0


@dataclass(slots=True)
class BlobRangeHandle:
    file_descriptor: int
    remaining_bytes: int
    size_bytes: int


class LocalBlobFiles:
    def __init__(self, storage_root: Path, workspace_id: str) -> None:
        self._storage_root = storage_root
        self._workspace_id = workspace_id
        self._root_descriptor = -1
        self._workspace_descriptor = -1
        self._blobs_descriptor = -1
        self._temporary_descriptor = -1

    def initialize(self) -> None:
        required_dir_fd_operations = (os.link, os.mkdir, os.open, os.unlink)
        if (
            os.name != "posix"
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or any(
                operation not in os.supports_dir_fd
                for operation in required_dir_fd_operations
            )
        ):
            raise BlobStoreError(BlobErrorCode.UNSUPPORTED_PLATFORM)
        root = self._storage_root
        if not root.is_absolute() or root.resolve(strict=True) != root:
            raise BlobStoreError(BlobErrorCode.INVALID_STORAGE)
        self._validate_private_directory(root.stat())
        directory_flags = (
            os.O_RDONLY | os.O_DIRECTORY | self._no_follow_flag()
        )
        self._root_descriptor = os.open(root, directory_flags)
        self._workspace_descriptor = self._ensure_directory(
            self._root_descriptor,
            self._workspace_id,
        )
        self._blobs_descriptor = self._ensure_directory(
            self._workspace_descriptor,
            "blobs",
        )
        self._temporary_descriptor = self._ensure_directory(
            self._workspace_descriptor,
            "temporary",
        )

    def begin(self) -> StagedBlob:
        temporary_name = f"pending-{secrets.token_hex(16)}"
        file_descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | self._no_follow_flag(),
            0o600,
            dir_fd=self._temporary_descriptor,
        )
        return StagedBlob(
            name=temporary_name,
            file_descriptor=file_descriptor,
            digest=hashlib.sha256(),
        )

    def available_bytes(self) -> int:
        filesystem = os.fstatvfs(self._workspace_descriptor)
        available_bytes = filesystem.f_bavail * filesystem.f_frsize
        return min(available_bytes, 2**63 - 1)

    @staticmethod
    def write(staged: StagedBlob, value: bytes, maximum_size: int) -> None:
        if not value or len(value) > MAXIMUM_IO_CHUNK_BYTES:
            raise BlobStoreError(BlobErrorCode.INVALID_CHUNK)
        next_size = staged.size_bytes + len(value)
        if next_size > maximum_size:
            raise BlobStoreError(BlobErrorCode.SIZE_MISMATCH)
        view = memoryview(value)
        while view:
            written = os.write(staged.file_descriptor, view)
            if written <= 0:
                raise BlobStoreError(BlobErrorCode.INVALID_STORAGE)
            view = view[written:]
        staged.digest.update(value)
        staged.size_bytes = next_size

    def publish(self, staged: StagedBlob, request: BlobWriteRequest) -> bool:
        if staged.size_bytes != request.expected_size_bytes:
            raise BlobStoreError(BlobErrorCode.SIZE_MISMATCH)
        if not hmac.compare_digest(
            staged.digest.hexdigest(),
            request.expected_content_sha256,
        ):
            raise BlobStoreError(BlobErrorCode.HASH_MISMATCH)
        os.fchmod(staged.file_descriptor, 0o400)
        os.fsync(staged.file_descriptor)
        shard_descriptor = self._shard_descriptor(
            request.expected_content_sha256,
            create=True,
        )
        created = True
        try:
            try:
                os.link(
                    staged.name,
                    request.expected_content_sha256,
                    src_dir_fd=self._temporary_descriptor,
                    dst_dir_fd=shard_descriptor,
                    follow_symlinks=False,
                )
                os.fsync(shard_descriptor)
            except FileExistsError:
                created = False
                verify_blob(
                    shard_descriptor,
                    request.expected_content_sha256,
                    request.expected_size_bytes,
                )
        finally:
            os.close(shard_descriptor)
        self.abort(staged)
        return created

    def abort(self, staged: StagedBlob) -> None:
        if staged.file_descriptor >= 0:
            os.close(staged.file_descriptor)
            staged.file_descriptor = -1
        try:
            os.unlink(staged.name, dir_fd=self._temporary_descriptor)
        except FileNotFoundError:
            pass

    def inspect(self, content_sha256: str) -> int:
        shard_descriptor = self._shard_descriptor(
            content_sha256,
            create=False,
        )
        try:
            return verify_blob(
                shard_descriptor,
                content_sha256,
                expected_size=None,
            )
        finally:
            os.close(shard_descriptor)

    def delete(self, content_sha256: str) -> bool:
        try:
            shard_descriptor = self._shard_descriptor(
                content_sha256,
                create=False,
            )
        except BlobStoreError as error:
            if error.code is BlobErrorCode.NOT_FOUND:
                return False
            raise
        try:
            try:
                try:
                    verify_blob(
                        shard_descriptor,
                        content_sha256,
                        expected_size=None,
                    )
                except BlobStoreError as error:
                    if error.code is BlobErrorCode.NOT_FOUND:
                        return False
                    raise
                os.unlink(content_sha256, dir_fd=shard_descriptor)
                os.fsync(shard_descriptor)
                return True
            except FileNotFoundError:
                return False
        finally:
            os.close(shard_descriptor)

    def open_range(
        self,
        content_sha256: str,
        offset_bytes: int,
        length_bytes: int,
    ) -> BlobRangeHandle:
        shard_descriptor = self._shard_descriptor(
            content_sha256,
            create=False,
        )
        try:
            file_descriptor, size_bytes = open_verified_blob(
                shard_descriptor,
                content_sha256,
            )
        finally:
            os.close(shard_descriptor)
        if offset_bytes >= size_bytes:
            os.close(file_descriptor)
            raise BlobStoreError(BlobErrorCode.RANGE_NOT_SATISFIABLE)
        os.lseek(file_descriptor, offset_bytes, os.SEEK_SET)
        return BlobRangeHandle(
            file_descriptor=file_descriptor,
            remaining_bytes=min(length_bytes, size_bytes - offset_bytes),
            size_bytes=size_bytes,
        )

    @staticmethod
    def read_range(handle: BlobRangeHandle, chunk_size: int) -> bytes:
        if handle.remaining_bytes == 0:
            return b""
        requested_bytes = min(handle.remaining_bytes, chunk_size)
        value = os.read(handle.file_descriptor, requested_bytes)
        if not value:
            raise BlobStoreError(BlobErrorCode.INVALID_STORAGE)
        handle.remaining_bytes -= len(value)
        return value

    @staticmethod
    def close_range(handle: BlobRangeHandle) -> None:
        if handle.file_descriptor >= 0:
            os.close(handle.file_descriptor)
            handle.file_descriptor = -1

    def close(self) -> None:
        descriptors = (
            self._temporary_descriptor,
            self._blobs_descriptor,
            self._workspace_descriptor,
            self._root_descriptor,
        )
        for descriptor in descriptors:
            if descriptor >= 0:
                os.close(descriptor)
        self._temporary_descriptor = -1
        self._blobs_descriptor = -1
        self._workspace_descriptor = -1
        self._root_descriptor = -1

    def _ensure_directory(self, parent_descriptor: int, name: str) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except FileExistsError:
            pass
        descriptor = self._open_directory(parent_descriptor, name)
        try:
            self._validate_private_directory(os.fstat(descriptor))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _shard_descriptor(self, content_sha256: str, *, create: bool) -> int:
        shard = content_sha256[:2]
        if create:
            return self._ensure_directory(self._blobs_descriptor, shard)
        try:
            descriptor = self._open_directory(self._blobs_descriptor, shard)
        except FileNotFoundError as error:
            raise BlobStoreError(BlobErrorCode.NOT_FOUND) from error
        try:
            self._validate_private_directory(os.fstat(descriptor))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_directory(self, parent_descriptor: int, name: str) -> int:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | self._no_follow_flag(),
            dir_fd=parent_descriptor,
        )

    @staticmethod
    def _validate_private_directory(status: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.getuid()
            or stat.S_IMODE(status.st_mode) & 0o077
        ):
            raise BlobStoreError(BlobErrorCode.INVALID_STORAGE)

    @staticmethod
    def _no_follow_flag() -> int:
        return int(getattr(os, "O_NOFOLLOW", 0))
