"""Atomic bounded workspace patch tests."""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.tools import (
    PatchFileArguments,
    PatchFileEdit,
    WorkspaceFileError,
    WorkspaceFileErrorCode,
    WorkspaceFileService,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_patch_updates_and_creates_atomically(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        target = workspace / "app.py"
        target.write_text("alpha\nbeta\n", encoding="utf-8")
        target.chmod(0o750)
        service = await WorkspaceFileService.open(workspace)
        try:
            updated = await service.patch(
                PatchFileArguments(
                    path="app.py",
                    expected_sha256=_sha256("alpha\nbeta\n"),
                    edits=(
                        PatchFileEdit(
                            old_text="beta",
                            new_text="gamma",
                        ),
                    ),
                ),
                cancellation=asyncio.Event(),
                timeout_ms=1000,
            )
            assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"
            assert updated.previous_sha256 == _sha256("alpha\nbeta\n")
            assert updated.content_sha256 == _sha256("alpha\ngamma\n")
            assert not updated.created
            assert target.stat().st_mode & 0o777 == 0o750

            created = await service.patch(
                PatchFileArguments(
                    path="created.txt",
                    expected_sha256=None,
                    edits=(PatchFileEdit(old_text="", new_text="new\n"),),
                ),
                cancellation=asyncio.Event(),
                timeout_ms=1000,
            )
            assert (workspace / "created.txt").read_text() == "new\n"
            assert created.previous_sha256 is None
            assert created.created
        finally:
            await service.close()

    asyncio.run(scenario())


def test_patch_conflicts_never_modify_target(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        target = workspace / "value.txt"
        target.write_text("same same", encoding="utf-8")
        service = await WorkspaceFileService.open(workspace)
        try:
            invalid_patches = (
                PatchFileArguments(
                    path="value.txt",
                    expected_sha256="0" * 64,
                    edits=(PatchFileEdit(old_text="same", new_text="new"),),
                ),
                PatchFileArguments(
                    path="value.txt",
                    expected_sha256=_sha256("same same"),
                    edits=(PatchFileEdit(old_text="same", new_text="new"),),
                ),
                PatchFileArguments(
                    path="value.txt",
                    expected_sha256=None,
                    edits=(PatchFileEdit(old_text="", new_text="new"),),
                ),
            )
            for arguments in invalid_patches:
                with pytest.raises(WorkspaceFileError) as failure:
                    await service.patch(
                        arguments,
                        cancellation=asyncio.Event(),
                        timeout_ms=1000,
                    )
                assert failure.value.code is WorkspaceFileErrorCode.CONFLICT
                assert target.read_text(encoding="utf-8") == "same same"
            assert not tuple(workspace.glob(".atlas-patch-*.tmp"))
        finally:
            await service.close()

    asyncio.run(scenario())


def test_patch_denies_symlink_and_does_not_modify_outside(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (workspace / "link").symlink_to(outside)
        service = await WorkspaceFileService.open(workspace)
        try:
            with pytest.raises(WorkspaceFileError) as failure:
                await service.patch(
                    PatchFileArguments(
                        path="link",
                        expected_sha256=_sha256("secret"),
                        edits=(
                            PatchFileEdit(
                                old_text="secret",
                                new_text="changed",
                            ),
                        ),
                    ),
                    cancellation=asyncio.Event(),
                    timeout_ms=1000,
                )
            assert failure.value.code is WorkspaceFileErrorCode.INVALID_PATH
            assert outside.read_text(encoding="utf-8") == "secret"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_failed_publication_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        target = workspace / "value.txt"
        target.write_text("before", encoding="utf-8")
        service = await WorkspaceFileService.open(workspace)

        def fail_replace(*args: object, **kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(os, "replace", fail_replace)
        try:
            with pytest.raises(WorkspaceFileError) as failure:
                await service.patch(
                    PatchFileArguments(
                        path="value.txt",
                        expected_sha256=_sha256("before"),
                        edits=(
                            PatchFileEdit(
                                old_text="before",
                                new_text="after",
                            ),
                        ),
                    ),
                    cancellation=asyncio.Event(),
                    timeout_ms=1000,
                )
            assert failure.value.code is WorkspaceFileErrorCode.IO
            assert target.read_text(encoding="utf-8") == "before"
            assert not tuple(workspace.glob(".atlas-patch-*.tmp"))
        finally:
            await service.close()

    asyncio.run(scenario())


def test_concurrent_change_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        target = workspace / "value.txt"
        target.write_text("before", encoding="utf-8")
        service = await WorkspaceFileService.open(workspace)
        original_fsync = os.fsync
        calls = 0

        def race_after_temporary_sync(descriptor: int) -> None:
            nonlocal calls
            original_fsync(descriptor)
            calls += 1
            if calls == 1:
                target.write_text("raced", encoding="utf-8")

        monkeypatch.setattr(os, "fsync", race_after_temporary_sync)
        try:
            with pytest.raises(WorkspaceFileError) as failure:
                await service.patch(
                    PatchFileArguments(
                        path="value.txt",
                        expected_sha256=_sha256("before"),
                        edits=(
                            PatchFileEdit(
                                old_text="before",
                                new_text="after",
                            ),
                        ),
                    ),
                    cancellation=asyncio.Event(),
                    timeout_ms=1000,
                )
            assert failure.value.code is WorkspaceFileErrorCode.CONFLICT
            assert target.read_text(encoding="utf-8") == "raced"
            assert not tuple(workspace.glob(".atlas-patch-*.tmp"))
        finally:
            await service.close()

    asyncio.run(scenario())


def test_post_publication_sync_failure_is_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        target = workspace / "value.txt"
        target.write_text("before", encoding="utf-8")
        service = await WorkspaceFileService.open(workspace)
        original_fsync = os.fsync
        calls = 0

        def fail_directory_sync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError
            original_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", fail_directory_sync)
        try:
            with pytest.raises(WorkspaceFileError) as failure:
                await service.patch(
                    PatchFileArguments(
                        path="value.txt",
                        expected_sha256=_sha256("before"),
                        edits=(
                            PatchFileEdit(
                                old_text="before",
                                new_text="after",
                            ),
                        ),
                    ),
                    cancellation=asyncio.Event(),
                    timeout_ms=1000,
                )
            assert failure.value.code is WorkspaceFileErrorCode.AMBIGUOUS
            assert target.read_text(encoding="utf-8") == "after"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_patch_contract_rejects_unsafe_shape() -> None:
    with pytest.raises(ValidationError, match="first patch edit"):
        PatchFileArguments(
            path="value.txt",
            expected_sha256=None,
            edits=(
                PatchFileEdit(old_text="a", new_text="b"),
                PatchFileEdit(old_text="", new_text="c"),
            ),
        )
