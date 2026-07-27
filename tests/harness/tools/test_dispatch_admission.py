"""Bounded duplicate-dispatch admission tests."""

import asyncio
import hashlib
from pathlib import Path

import pytest

from app.services.harness.sandbox import SandboxProcessResult
from app.services.harness.tools import (
    PATCH_FILE_TOOL_NAME,
    SandboxedProcessService,
    ToolDispatchAdmissionError,
    ToolDispatchAdmissionErrorCode,
    WorkspaceFileService,
    builtin_tool_registry,
)
from app.services.harness.tools.dispatch_admission import OperationClaimGuard
from app.services.harness.tools.dispatch_service import BuiltinToolDispatcher
from tests.harness.tools.dispatch_fixtures import (
    WORKSPACE_ID,
    catalog_call,
    durable_dispatch,
    terminal_time,
)
from tests.harness.tools.process_fixtures import RecordingSupervisor


def test_replayed_permit_never_reaches_patch_twice(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path.resolve()
        target = workspace / "value.txt"
        target.write_text("before", encoding="utf-8")
        call = catalog_call(
            PATCH_FILE_TOOL_NAME,
            {
                "edits": [{"new_text": "after", "old_text": "before"}],
                "expected_sha256": hashlib.sha256(b"before").hexdigest(),
                "path": "value.txt",
            },
            call_id="call-replay",
        )
        permit, lifecycle, store = await durable_dispatch(
            call,
            suffix="replay",
        )
        files = await WorkspaceFileService.open(workspace)
        dispatcher = BuiltinToolDispatcher(
            builtin_tool_registry(),
            files,
            SandboxedProcessService(
                RecordingSupervisor(
                    SandboxProcessResult(
                        return_code=0,
                        stdout=b"",
                        stderr=b"",
                    )
                )
            ),
            lifecycle,
            workspace_id=WORKSPACE_ID,
            clock=terminal_time,
        )
        try:
            await dispatcher.dispatch(
                call,
                permit,
                cancellation=asyncio.Event(),
            )
            with pytest.raises(ToolDispatchAdmissionError) as replay:
                await dispatcher.dispatch(
                    call,
                    permit,
                    cancellation=asyncio.Event(),
                )
            assert replay.value.code is ToolDispatchAdmissionErrorCode.DUPLICATE
            assert target.read_text(encoding="utf-8") == "after"
            assert len(store.records) == 3
        finally:
            await files.close()

    asyncio.run(scenario())


def test_claim_capacity_is_bounded_and_duplicate_takes_precedence() -> None:
    async def scenario() -> None:
        guard = OperationClaimGuard(maximum_claimed_operations=1)
        await guard.claim("opn_" + "1" * 32)
        with pytest.raises(ToolDispatchAdmissionError) as duplicate:
            await guard.claim("opn_" + "1" * 32)
        assert duplicate.value.code is ToolDispatchAdmissionErrorCode.DUPLICATE
        with pytest.raises(ToolDispatchAdmissionError) as capacity:
            await guard.claim("opn_" + "2" * 32)
        assert capacity.value.code is ToolDispatchAdmissionErrorCode.CAPACITY

    asyncio.run(scenario())
