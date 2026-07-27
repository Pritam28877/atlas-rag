"""Atomic hash-preconditioned workspace patch publication."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass

from app.services.harness.tools.file_contracts import (
    MAXIMUM_PATCH_FILE_BYTES,
    PatchFileArguments,
    PatchFileResult,
)
from app.services.harness.tools.file_errors import (
    WorkspaceFileError,
    WorkspaceFileErrorCode,
)


@dataclass(frozen=True, slots=True)
class _PatchSource:
    content: bytes
    metadata: os.stat_result
    sha256: str


def patch_workspace_file(
    parent_descriptor: int,
    name: str,
    arguments: PatchFileArguments,
    *,
    check_runtime: Callable[[], None],
) -> PatchFileResult:
    temporary_name: str | None = None
    published = False
    try:
        source = _read_source(
            parent_descriptor,
            name,
            arguments.expected_sha256,
            check_runtime=check_runtime,
        )
        replacement = _apply_edits(
            source.content if source is not None else b"",
            arguments,
        )
        temporary_name = _write_temporary(
            parent_descriptor,
            replacement,
            source.metadata if source is not None else None,
            check_runtime=check_runtime,
        )
        check_runtime()
        if source is None:
            os.link(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            published = True
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        else:
            _require_unchanged(
                parent_descriptor,
                name,
                source,
                check_runtime=check_runtime,
            )
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            published = True
        temporary_name = None
        os.fsync(parent_descriptor)
        return PatchFileResult(
            path=arguments.path,
            previous_sha256=source.sha256 if source is not None else None,
            content_sha256=hashlib.sha256(replacement).hexdigest(),
            content_bytes=len(replacement),
            edits_applied=len(arguments.edits),
            created=source is None,
        )
    except FileExistsError:
        raise WorkspaceFileError(WorkspaceFileErrorCode.CONFLICT) from None
    except WorkspaceFileError:
        raise
    except OSError:
        code = (
            WorkspaceFileErrorCode.AMBIGUOUS
            if published
            else WorkspaceFileErrorCode.IO
        )
        raise WorkspaceFileError(code) from None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def _read_source(
    parent_descriptor: int,
    name: str,
    expected_sha256: str | None,
    *,
    check_runtime: Callable[[], None],
) -> _PatchSource | None:
    try:
        descriptor = _open_regular_file(parent_descriptor, name)
    except FileNotFoundError:
        if expected_sha256 is None:
            return None
        raise WorkspaceFileError(WorkspaceFileErrorCode.NOT_FOUND) from None
    try:
        metadata = os.fstat(descriptor)
        content = _read_bounded(
            descriptor,
            check_runtime=check_runtime,
        )
    finally:
        os.close(descriptor)
    if expected_sha256 is None:
        raise WorkspaceFileError(WorkspaceFileErrorCode.CONFLICT)
    if len(content) > MAXIMUM_PATCH_FILE_BYTES:
        raise WorkspaceFileError(WorkspaceFileErrorCode.LIMIT)
    _decode_text(content)
    content_sha256 = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(content_sha256, expected_sha256):
        raise WorkspaceFileError(WorkspaceFileErrorCode.CONFLICT)
    return _PatchSource(
        content=content,
        metadata=metadata,
        sha256=content_sha256,
    )


def _apply_edits(source: bytes, arguments: PatchFileArguments) -> bytes:
    text = _decode_text(source)
    for edit in arguments.edits:
        if not edit.old_text:
            if text:
                raise WorkspaceFileError(WorkspaceFileErrorCode.CONFLICT)
            text = edit.new_text
        else:
            if text.count(edit.old_text) != 1:
                raise WorkspaceFileError(WorkspaceFileErrorCode.CONFLICT)
            text = text.replace(edit.old_text, edit.new_text, 1)
        if len(text.encode()) > MAXIMUM_PATCH_FILE_BYTES:
            raise WorkspaceFileError(WorkspaceFileErrorCode.LIMIT)
    return text.encode()


def _write_temporary(
    parent_descriptor: int,
    content: bytes,
    source_metadata: os.stat_result | None,
    *,
    check_runtime: Callable[[], None],
) -> str:
    name = f".atlas-patch-{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        if source_metadata is not None:
            os.fchmod(descriptor, stat.S_IMODE(source_metadata.st_mode) & 0o777)
        offset = 0
        while offset < len(content):
            check_runtime()
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("workspace patch write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        finally:
            _remove_temporary(parent_descriptor, name)
        raise
    try:
        os.close(descriptor)
    except BaseException:
        _remove_temporary(parent_descriptor, name)
        raise
    return name


def _require_unchanged(
    parent_descriptor: int,
    name: str,
    source: _PatchSource,
    *,
    check_runtime: Callable[[], None],
) -> None:
    try:
        descriptor = _open_regular_file(parent_descriptor, name)
    except (OSError, WorkspaceFileError):
        raise WorkspaceFileError(WorkspaceFileErrorCode.CONFLICT) from None
    try:
        metadata = os.fstat(descriptor)
        content = _read_bounded(
            descriptor,
            check_runtime=check_runtime,
        )
    finally:
        os.close(descriptor)
    if (
        metadata.st_dev != source.metadata.st_dev
        or metadata.st_ino != source.metadata.st_ino
        or len(content) > MAXIMUM_PATCH_FILE_BYTES
        or not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(),
            source.sha256,
        )
    ):
        raise WorkspaceFileError(WorkspaceFileErrorCode.CONFLICT)


def _open_regular_file(parent_descriptor: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        raise
    except OSError:
        raise WorkspaceFileError(
            WorkspaceFileErrorCode.INVALID_PATH
        ) from None
    try:
        is_regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
    except BaseException:
        os.close(descriptor)
        raise
    if not is_regular:
        os.close(descriptor)
        raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_PATH)
    return descriptor


def _read_bounded(
    descriptor: int,
    *,
    check_runtime: Callable[[], None],
) -> bytes:
    content = bytearray()
    maximum_bytes = MAXIMUM_PATCH_FILE_BYTES + 1
    while len(content) < maximum_bytes:
        check_runtime()
        requested = min(64 * 1024, maximum_bytes - len(content))
        chunk = os.pread(descriptor, requested, len(content))
        if not chunk:
            break
        content.extend(chunk)
    return bytes(content)


def _remove_temporary(parent_descriptor: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        pass


def _decode_text(content: bytes) -> str:
    if b"\x00" in content:
        raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_TEXT)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkspaceFileError(
            WorkspaceFileErrorCode.INVALID_TEXT
        ) from None
