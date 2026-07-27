"""POSIX descriptor-relative workspace reads without symlink traversal."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.services.harness.tools.file_contracts import (
    MAXIMUM_MATCH_TEXT_BYTES,
    PatchFileArguments,
    PatchFileResult,
    ReadFileArguments,
    ReadFileResult,
    SearchFilesArguments,
    SearchFilesResult,
    SearchMatch,
    validate_workspace_relative_path,
)
from app.services.harness.tools.file_errors import (
    WorkspaceFileError,
    WorkspaceFileErrorCode,
)
from app.services.harness.tools.workspace_patch import patch_workspace_file


@dataclass(slots=True)
class _SearchState:
    matches: list[SearchMatch]
    scanned_files: int = 0
    scanned_bytes: int = 0
    skipped_binary_files: int = 0
    truncated: bool = False


class WorkspaceDescriptor:
    def __init__(self, workspace: Path) -> None:
        self._root_descriptor = -1
        if (
            os.name != "posix"
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd
        ):
            raise WorkspaceFileError(WorkspaceFileErrorCode.UNSUPPORTED)
        try:
            if (
                not workspace.is_absolute()
                or workspace.resolve(strict=True) != workspace
                or not workspace.is_dir()
            ):
                raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_PATH)
            self._root_descriptor = os.open(
                workspace,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
        except WorkspaceFileError:
            raise
        except OSError:
            raise WorkspaceFileError(WorkspaceFileErrorCode.IO) from None

    def read(
        self,
        arguments: ReadFileArguments,
        *,
        stop: threading.Event,
        deadline: float,
    ) -> ReadFileResult:
        self._check_runtime(stop, deadline)
        descriptor = self._open_file(arguments.path)
        try:
            metadata = os.fstat(descriptor)
            requested = arguments.maximum_bytes + 1
            value = os.pread(
                descriptor,
                requested,
                arguments.offset_bytes,
            )
        except OSError:
            raise WorkspaceFileError(WorkspaceFileErrorCode.IO) from None
        finally:
            os.close(descriptor)
        self._check_runtime(stop, deadline)
        truncated = len(value) > arguments.maximum_bytes
        content = value[: arguments.maximum_bytes]
        if b"\x00" in content:
            raise WorkspaceFileError(
                WorkspaceFileErrorCode.INVALID_TEXT
            )
        text, content = _decode_utf8_content(
            content,
            allow_incomplete_tail=truncated,
        )
        return ReadFileResult(
            path=arguments.path,
            offset_bytes=arguments.offset_bytes,
            file_size_bytes=metadata.st_size,
            content=text,
            content_bytes=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
            truncated=truncated,
        )

    def search(
        self,
        arguments: SearchFilesArguments,
        *,
        stop: threading.Event,
        deadline: float,
    ) -> SearchFilesResult:
        self._check_runtime(stop, deadline)
        start_parts = validate_workspace_relative_path(
            arguments.path,
            allow_root=True,
        )
        start_descriptor = self._open_directory_parts(start_parts)
        state = _SearchState(matches=[])
        pending = [(start_parts, start_descriptor)]
        try:
            while pending:
                self._check_runtime(stop, deadline)
                relative_parts, directory_descriptor = pending.pop()
                try:
                    with os.scandir(directory_descriptor) as iterator:
                        entries = sorted(
                            iterator,
                            key=lambda entry: entry.name,
                            reverse=True,
                        )
                    for entry in entries:
                        self._check_runtime(stop, deadline)
                        parts = (*relative_parts, entry.name)
                        if entry.is_dir(follow_symlinks=False):
                            child = self._open_directory_at(
                                directory_descriptor,
                                entry.name,
                            )
                            pending.append((parts, child))
                        elif entry.is_file(follow_symlinks=False):
                            if not self._search_file(
                                directory_descriptor,
                                entry.name,
                                parts,
                                arguments,
                                state,
                            ):
                                break
                    if state.truncated:
                        break
                finally:
                    os.close(directory_descriptor)
            return SearchFilesResult(
                matches=tuple(state.matches),
                scanned_files=state.scanned_files,
                scanned_bytes=state.scanned_bytes,
                skipped_binary_files=state.skipped_binary_files,
                truncated=state.truncated,
            )
        finally:
            for _, descriptor in pending:
                os.close(descriptor)

    def patch(
        self,
        arguments: PatchFileArguments,
        *,
        stop: threading.Event,
        deadline: float,
    ) -> PatchFileResult:
        self._check_runtime(stop, deadline)
        parts = validate_workspace_relative_path(arguments.path)
        parent = self._open_directory_parts(parts[:-1])
        try:
            return patch_workspace_file(
                parent,
                parts[-1],
                arguments,
                check_runtime=lambda: self._check_runtime(stop, deadline),
            )
        finally:
            os.close(parent)

    def close(self) -> None:
        if self._root_descriptor >= 0:
            os.close(self._root_descriptor)
            self._root_descriptor = -1

    def _search_file(
        self,
        parent_descriptor: int,
        name: str,
        parts: tuple[str, ...],
        arguments: SearchFilesArguments,
        state: _SearchState,
    ) -> bool:
        if (
            state.scanned_files >= arguments.maximum_files
            or state.scanned_bytes >= arguments.maximum_bytes
        ):
            state.truncated = True
            return False
        descriptor = self._open_at(parent_descriptor, name, directory=False)
        remaining = arguments.maximum_bytes - state.scanned_bytes
        requested = min(remaining, 1024 * 1024)
        try:
            value = os.read(descriptor, requested + 1)
        except OSError:
            raise WorkspaceFileError(WorkspaceFileErrorCode.IO) from None
        finally:
            os.close(descriptor)
        state.scanned_files += 1
        consumed = value[:requested]
        state.scanned_bytes += len(consumed)
        file_truncated = len(value) > requested
        if file_truncated:
            state.truncated = True
        if b"\x00" in consumed:
            state.skipped_binary_files += 1
            return True
        try:
            text, _ = _decode_utf8_content(
                consumed,
                allow_incomplete_tail=file_truncated,
            )
        except WorkspaceFileError:
            state.skipped_binary_files += 1
            return True
        query = (
            arguments.query
            if arguments.case_sensitive
            else arguments.query.casefold()
        )
        for line_number, line in enumerate(text.splitlines(), start=1):
            candidate = line if arguments.case_sensitive else line.casefold()
            if query not in candidate:
                continue
            line_value, line_truncated = _truncate_utf8(
                line,
                MAXIMUM_MATCH_TEXT_BYTES,
            )
            state.matches.append(
                SearchMatch(
                    path="/".join(parts),
                    line_number=line_number,
                    line=line_value,
                    line_bytes=len(line_value.encode()),
                    truncated=line_truncated,
                )
            )
            if len(state.matches) >= arguments.maximum_matches:
                state.truncated = True
                return False
        return not state.truncated

    def _open_file(self, path: str) -> int:
        parts = validate_workspace_relative_path(path)
        parent = self._open_directory_parts(parts[:-1])
        try:
            descriptor = self._open_at(parent, parts[-1], directory=False)
        finally:
            if parent != self._root_descriptor:
                os.close(parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise WorkspaceFileError(WorkspaceFileErrorCode.INVALID_PATH)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_directory_parts(self, parts: tuple[str, ...]) -> int:
        if self._root_descriptor < 0:
            raise WorkspaceFileError(WorkspaceFileErrorCode.CLOSED)
        current = os.dup(self._root_descriptor)
        try:
            for part in parts:
                child = self._open_directory_at(current, part)
                os.close(current)
                current = child
            return current
        except BaseException:
            os.close(current)
            raise

    def _open_directory_at(self, parent: int, name: str) -> int:
        return self._open_at(parent, name, directory=True)

    def _open_at(self, parent: int, name: str, *, directory: bool) -> int:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        if directory:
            flags |= os.O_DIRECTORY
        try:
            return os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            raise WorkspaceFileError(WorkspaceFileErrorCode.NOT_FOUND) from None
        except (NotADirectoryError, IsADirectoryError, OSError):
            raise WorkspaceFileError(
                WorkspaceFileErrorCode.INVALID_PATH
            ) from None

    @staticmethod
    def _check_runtime(stop: threading.Event, deadline: float) -> None:
        if stop.is_set():
            raise WorkspaceFileError(WorkspaceFileErrorCode.CANCELLED)
        if time.monotonic() >= deadline:
            raise WorkspaceFileError(WorkspaceFileErrorCode.TIMEOUT)


def _truncate_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.encode()
    if len(encoded) <= maximum_bytes:
        return value, False
    shortened = encoded[:maximum_bytes]
    while shortened:
        try:
            return shortened.decode("utf-8"), True
        except UnicodeDecodeError as error:
            shortened = shortened[: error.start]
    return "", True


def _decode_utf8_content(
    value: bytes,
    *,
    allow_incomplete_tail: bool,
) -> tuple[str, bytes]:
    try:
        return value.decode("utf-8"), value
    except UnicodeDecodeError as error:
        incomplete_tail = (
            allow_incomplete_tail
            and error.end == len(value)
            and error.reason == "unexpected end of data"
        )
        if incomplete_tail:
            complete_value = value[: error.start]
            return complete_value.decode("utf-8"), complete_value
        raise WorkspaceFileError(
            WorkspaceFileErrorCode.INVALID_TEXT
        ) from None
