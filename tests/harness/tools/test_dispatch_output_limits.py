"""Dispatcher result-filter boundary tests."""

import asyncio
import json
from pathlib import Path

from app.services.harness.sandbox import SandboxProcessResult
from app.services.harness.tools import (
    SEARCH_FILES_TOOL_NAME,
    SandboxedProcessService,
    WorkspaceFileService,
    builtin_tool_registry,
)
from app.services.harness.tools.dispatch_contracts import ToolDispatchStatus
from app.services.harness.tools.dispatch_service import BuiltinToolDispatcher
from tests.harness.tools.dispatch_fixtures import (
    WORKSPACE_ID,
    catalog_call,
    durable_dispatch,
    terminal_time,
)
from tests.harness.tools.process_fixtures import RecordingSupervisor


def test_oversized_read_only_result_fails_durably(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path.resolve()
        long_line = "needle " + "x" * 3990
        (workspace / "large.txt").write_text(
            "\n".join(long_line for _ in range(300)),
            encoding="utf-8",
        )
        call = catalog_call(
            SEARCH_FILES_TOOL_NAME,
            {
                "maximum_bytes": 2 * 1024 * 1024,
                "maximum_files": 10,
                "maximum_matches": 300,
                "path": ".",
                "query": "needle",
            },
            call_id="call-large-search",
        )
        permit, lifecycle, store = await durable_dispatch(
            call,
            suffix="large-search",
        )
        files = await WorkspaceFileService.open(workspace)
        try:
            outcome = await BuiltinToolDispatcher(
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
            ).dispatch(
                call,
                permit,
                cancellation=asyncio.Event(),
            )
            response = json.loads(outcome.response_json)
            assert outcome.status is ToolDispatchStatus.FAILED
            assert response["error"]["code"] == "result_limit"
            assert store.records[-1][1] == outcome.operation
        finally:
            await files.close()

    asyncio.run(scenario())
