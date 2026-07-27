"""Descriptor-safe bounded workspace read and search tests."""

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.tools.file_contracts import (
    ReadFileArguments,
    SearchFilesArguments,
)
from app.services.harness.tools.file_service import WorkspaceFileService
from app.services.harness.tools.workspace_descriptor import (
    WorkspaceFileError,
    WorkspaceFileErrorCode,
)


def test_read_is_bounded_and_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src/app.py").write_text("alpha\nbeta\n", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (workspace / "src/link").symlink_to(outside)
        service = await WorkspaceFileService.open(workspace)
        try:
            result = await service.read(
                ReadFileArguments(
                    path="src/app.py",
                    maximum_bytes=5,
                ),
                cancellation=asyncio.Event(),
                timeout_ms=1000,
            )
            assert result.content == "alpha"
            assert result.content_bytes == 5
            assert result.file_size_bytes == 11
            assert result.truncated

            empty = await service.read(
                ReadFileArguments(
                    path="src/app.py",
                    offset_bytes=100,
                    maximum_bytes=5,
                ),
                cancellation=asyncio.Event(),
                timeout_ms=1000,
            )
            assert empty.content == ""
            assert not empty.truncated

            with pytest.raises(WorkspaceFileError) as escaped:
                await service.read(
                    ReadFileArguments(path="src/link"),
                    cancellation=asyncio.Event(),
                    timeout_ms=1000,
                )
            assert escaped.value.code is WorkspaceFileErrorCode.INVALID_PATH
        finally:
            await service.close()

    asyncio.run(scenario())


def test_search_is_deterministic_bounded_and_skips_binary(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        (workspace / "src/nested").mkdir(parents=True)
        (workspace / "src/a.txt").write_text(
            "Needle one\nnone\n",
            encoding="utf-8",
        )
        (workspace / "src/nested/b.txt").write_text(
            "needle two\nneedle three\n",
            encoding="utf-8",
        )
        (workspace / "src/data.bin").write_bytes(b"\xff\x00needle")
        (workspace / "nul.txt").write_bytes(b"needle\x00hidden")
        service = await WorkspaceFileService.open(workspace)
        try:
            result = await service.search(
                SearchFilesArguments(
                    query="needle",
                    case_sensitive=False,
                    maximum_matches=2,
                ),
                cancellation=asyncio.Event(),
                timeout_ms=1000,
            )
            assert [(match.path, match.line_number) for match in result.matches] == [
                ("src/a.txt", 1),
                ("src/nested/b.txt", 1),
            ]
            assert result.skipped_binary_files == 2
            assert result.truncated

            empty = await service.search(
                SearchFilesArguments(path="src", query="absent"),
                cancellation=asyncio.Event(),
                timeout_ms=1000,
            )
            assert empty.matches == ()
            assert not empty.truncated
        finally:
            await service.close()

    asyncio.run(scenario())


def test_read_preserves_valid_utf8_at_byte_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        (workspace / "utf8.txt").write_text("a€b", encoding="utf-8")
        service = await WorkspaceFileService.open(workspace)
        try:
            result = await service.read(
                ReadFileArguments(path="utf8.txt", maximum_bytes=2),
                cancellation=asyncio.Event(),
                timeout_ms=1000,
            )
            assert result.content == "a"
            assert result.content_bytes == 1
            assert result.truncated
        finally:
            await service.close()

    asyncio.run(scenario())


def test_traversal_and_symlinked_directory_are_denied(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="canonical"):
        ReadFileArguments(path="../outside")
    with pytest.raises(ValidationError, match="byte limit"):
        SearchFilesArguments(query="€" * 4096)

    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
        service = await WorkspaceFileService.open(workspace)
        try:
            with pytest.raises(WorkspaceFileError) as failure:
                await service.search(
                    SearchFilesArguments(path="linked", query="secret"),
                    cancellation=asyncio.Event(),
                    timeout_ms=1000,
                )
            assert failure.value.code is WorkspaceFileErrorCode.INVALID_PATH
        finally:
            await service.close()

    asyncio.run(scenario())


class _BlockingBackend:
    def __init__(self) -> None:
        self.stopped = threading.Event()

    def read(self, arguments, *, stop, deadline):
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        self.stopped.set()
        code = (
            WorkspaceFileErrorCode.CANCELLED
            if stop.is_set()
            else WorkspaceFileErrorCode.TIMEOUT
        )
        raise WorkspaceFileError(code)

    def search(self, arguments, *, stop, deadline):
        return self.read(arguments, stop=stop, deadline=deadline)

    def close(self) -> None:
        return None


def test_timeout_and_cancellation_stop_owned_worker() -> None:
    async def scenario() -> None:
        timeout_backend = _BlockingBackend()
        timeout_service = WorkspaceFileService(
            timeout_backend,
            ThreadPoolExecutor(max_workers=1),
            maximum_pending_operations=1,
        )
        with pytest.raises(WorkspaceFileError) as timed_out:
            await timeout_service.read(
                ReadFileArguments(path="src/app.py"),
                cancellation=asyncio.Event(),
                timeout_ms=10,
            )
        assert timed_out.value.code is WorkspaceFileErrorCode.TIMEOUT
        assert timeout_backend.stopped.is_set()
        await timeout_service.close()

        cancellation_backend = _BlockingBackend()
        cancellation_service = WorkspaceFileService(
            cancellation_backend,
            ThreadPoolExecutor(max_workers=1),
            maximum_pending_operations=1,
        )
        cancellation = asyncio.Event()
        operation = asyncio.create_task(
            cancellation_service.read(
                ReadFileArguments(path="src/app.py"),
                cancellation=cancellation,
                timeout_ms=1000,
            )
        )
        await asyncio.sleep(0.01)
        cancellation.set()
        with pytest.raises(WorkspaceFileError) as cancelled:
            await operation
        assert cancelled.value.code is WorkspaceFileErrorCode.CANCELLED
        assert cancellation_backend.stopped.is_set()
        await cancellation_service.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="descriptor-relative POSIX boundary",
)
def test_binary_read_is_rejected_without_content_leak(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        (workspace / "binary").write_bytes(b"\xffsecret")
        service = await WorkspaceFileService.open(workspace)
        try:
            with pytest.raises(WorkspaceFileError) as failure:
                await service.read(
                    ReadFileArguments(path="binary"),
                    cancellation=asyncio.Event(),
                    timeout_ms=1000,
                )
            assert failure.value.code is WorkspaceFileErrorCode.INVALID_TEXT
            assert "secret" not in str(failure.value)
        finally:
            await service.close()

    asyncio.run(scenario())
