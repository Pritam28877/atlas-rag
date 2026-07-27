"""Workspace patch size and text-boundary tests."""

import asyncio
import hashlib
from pathlib import Path

import pytest

from app.services.harness.tools import (
    PatchFileArguments,
    PatchFileEdit,
    WorkspaceFileError,
    WorkspaceFileErrorCode,
    WorkspaceFileService,
)


def test_patch_rejects_missing_binary_and_oversized_sources(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = (tmp_path / "workspace").resolve()
        workspace.mkdir()
        binary = b"\xffsecret"
        oversized = b"a" * (1024 * 1024 + 1)
        (workspace / "binary").write_bytes(binary)
        (workspace / "oversized").write_bytes(oversized)
        service = await WorkspaceFileService.open(workspace)
        cases = (
            (
                PatchFileArguments(
                    path="missing",
                    expected_sha256="0" * 64,
                    edits=(PatchFileEdit(old_text="a", new_text="b"),),
                ),
                WorkspaceFileErrorCode.NOT_FOUND,
            ),
            (
                PatchFileArguments(
                    path="binary",
                    expected_sha256=hashlib.sha256(binary).hexdigest(),
                    edits=(
                        PatchFileEdit(old_text="secret", new_text="hidden"),
                    ),
                ),
                WorkspaceFileErrorCode.INVALID_TEXT,
            ),
            (
                PatchFileArguments(
                    path="oversized",
                    expected_sha256=hashlib.sha256(oversized).hexdigest(),
                    edits=(PatchFileEdit(old_text="a", new_text="b"),),
                ),
                WorkspaceFileErrorCode.LIMIT,
            ),
        )
        try:
            for arguments, expected_code in cases:
                with pytest.raises(WorkspaceFileError) as failure:
                    await service.patch(
                        arguments,
                        cancellation=asyncio.Event(),
                        timeout_ms=1000,
                    )
                assert failure.value.code is expected_code
        finally:
            await service.close()

    asyncio.run(scenario())
