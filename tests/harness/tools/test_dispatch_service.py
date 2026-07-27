"""Durable happy-path built-in dispatch tests."""

import asyncio
import hashlib
import json
from pathlib import Path

from app.services.harness.sandbox import (
    SandboxProcessResult,
)
from app.services.harness.tools import (
    PATCH_FILE_TOOL_NAME,
    PROCESS_TOOL_NAME,
    READ_FILE_TOOL_NAME,
    SEARCH_FILES_TOOL_NAME,
    SandboxedProcessService,
    WorkspaceFileService,
    builtin_tool_registry,
)
from app.services.harness.tools.builtin_executor import ProcessDispatchContext
from app.services.harness.tools.dispatch_contracts import ToolDispatchStatus
from app.services.harness.tools.dispatch_service import (
    BuiltinToolDispatcher,
)
from app.services.harness.tools.operation_lifecycle import (
    DurableOperationLifecycle,
)
from tests.harness.operations.fixtures import RecordingStore
from tests.harness.tools.dispatch_fixtures import (
    WORKSPACE_ID,
    catalog_call,
    durable_dispatch,
    terminal_time,
)
from tests.harness.tools.process_fixtures import (
    RecordingSupervisor,
    process_evidence,
)


def test_read_search_and_patch_complete_durably(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path.resolve()
        target = workspace / "value.txt"
        target.write_text("needle before\n", encoding="utf-8")
        files = await WorkspaceFileService.open(workspace)
        processes = SandboxedProcessService(
            RecordingSupervisor(
                SandboxProcessResult(return_code=0, stdout=b"", stderr=b"")
            )
        )
        try:
            calls = (
                catalog_call(
                    READ_FILE_TOOL_NAME,
                    {"path": "value.txt"},
                    call_id="call-read",
                ),
                catalog_call(
                    SEARCH_FILES_TOOL_NAME,
                    {"path": ".", "query": "needle"},
                    call_id="call-search",
                ),
                catalog_call(
                    PATCH_FILE_TOOL_NAME,
                    {
                        "edits": [
                            {
                                "new_text": "after",
                                "old_text": "before",
                            }
                        ],
                        "expected_sha256": hashlib.sha256(
                            b"needle before\n"
                        ).hexdigest(),
                        "path": "value.txt",
                    },
                    call_id="call-patch",
                ),
            )
            for index, call in enumerate(calls, start=1):
                permit, lifecycle, store = await durable_dispatch(
                    call,
                    suffix=str(index),
                )
                dispatcher = BuiltinToolDispatcher(
                    builtin_tool_registry(),
                    files,
                    processes,
                    lifecycle,
                    workspace_id=WORKSPACE_ID,
                    clock=terminal_time,
                )
                outcome = await dispatcher.dispatch(
                    call,
                    permit,
                    cancellation=asyncio.Event(),
                )
                assert outcome.status is ToolDispatchStatus.COMPLETED
                assert outcome.operation == store.records[-1][1]
                assert outcome.execution_result_sha256
                assert json.loads(outcome.response_json)
            assert target.read_text(encoding="utf-8") == "needle after\n"
        finally:
            await files.close()

    asyncio.run(scenario())


def test_process_completes_through_permit_bound_context(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path.resolve()
        call = catalog_call(
            PROCESS_TOOL_NAME,
            {
                "arguments": ["-c", "print('safe')"],
                "executable": "/usr/bin/python3",
                "working_directory": "work",
            },
            call_id="call-process",
        )
        call, permit, profile, admission, environment = await process_evidence(
            workspace,
            call=call,
        )
        files = await WorkspaceFileService.open(workspace)
        store = RecordingStore()
        supervisor = RecordingSupervisor(
            SandboxProcessResult(
                return_code=0,
                stdout=b"ok\n",
                stderr=b"",
            )
        )
        try:
            outcome = await BuiltinToolDispatcher(
                builtin_tool_registry(),
                files,
                SandboxedProcessService(supervisor),
                lifecycle=DurableOperationLifecycle(store),
                workspace_id=WORKSPACE_ID,
                clock=terminal_time,
            ).dispatch(
                call,
                permit,
                cancellation=asyncio.Event(),
                process=ProcessDispatchContext(
                    profile=profile,
                    admission=admission,
                    environment=environment,
                ),
            )
            assert outcome.status is ToolDispatchStatus.COMPLETED
            assert json.loads(outcome.response_json)["stdout"] == "ok\n"
            assert store.records[-1][1] == outcome.operation
            assert supervisor.calls == 1
        finally:
            await files.close()

    asyncio.run(scenario())
