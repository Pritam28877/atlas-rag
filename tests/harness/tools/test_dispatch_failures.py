"""Durable denial, cancellation, ambiguity, and persistence tests."""

import asyncio
from pathlib import Path

import pytest

from app.services.harness.sandbox import (
    SandboxProcessResult,
    SandboxProcessTimedOut,
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
    ToolDispatchDurabilityError,
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


def _processes(
    outcome: SandboxProcessResult | BaseException | None = None,
) -> SandboxedProcessService:
    value = outcome or SandboxProcessResult(
        return_code=0,
        stdout=b"",
        stderr=b"",
    )
    return SandboxedProcessService(RecordingSupervisor(value))


def test_file_conflict_and_precancel_are_durable_terminal_outcomes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path.resolve()
        (workspace / "value.txt").write_text("current", encoding="utf-8")
        files = await WorkspaceFileService.open(workspace)
        try:
            conflict_call = catalog_call(
                PATCH_FILE_TOOL_NAME,
                {
                    "edits": [{"new_text": "new", "old_text": "current"}],
                    "expected_sha256": "0" * 64,
                    "path": "value.txt",
                },
                call_id="call-conflict",
            )
            read_call = catalog_call(
                READ_FILE_TOOL_NAME,
                {"path": "value.txt"},
                call_id="call-cancel",
            )
            for suffix, call, cancellation, expected in (
                (
                    "conflict",
                    conflict_call,
                    asyncio.Event(),
                    ToolDispatchStatus.FAILED,
                ),
                (
                    "cancel",
                    read_call,
                    _set_event(),
                    ToolDispatchStatus.CANCELLED,
                ),
            ):
                permit, lifecycle, store = await durable_dispatch(
                    call,
                    suffix=suffix,
                )
                outcome = await BuiltinToolDispatcher(
                    builtin_tool_registry(),
                    files,
                    _processes(),
                    lifecycle,
                    workspace_id=WORKSPACE_ID,
                    clock=terminal_time,
                ).dispatch(
                    call,
                    permit,
                    cancellation=cancellation,
                )
                assert outcome.status is expected
                assert store.records[-1][1] == outcome.operation
                assert not outcome.retry_allowed
            assert (workspace / "value.txt").read_text() == "current"
        finally:
            await files.close()

    asyncio.run(scenario())


def test_process_timeout_is_ambiguous_and_missing_context_fails(
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
            call_id="call-process-failure",
        )
        call, permit, profile, admission, environment = await process_evidence(
            workspace,
            call=call,
        )
        files = await WorkspaceFileService.open(workspace)
        try:
            for suffix, process_context, process_service, expected in (
                (
                    "timeout",
                    ProcessDispatchContext(profile, admission, environment),
                    _processes(SandboxProcessTimedOut("sensitive")),
                    ToolDispatchStatus.AMBIGUOUS,
                ),
                (
                    "missing",
                    None,
                    _processes(),
                    ToolDispatchStatus.FAILED,
                ),
            ):
                if suffix == "timeout":
                    dispatch_permit = permit
                else:
                    dispatch_permit, _, _ = await durable_dispatch(
                        call,
                        suffix=suffix,
                    )
                store = RecordingStore()
                outcome = await BuiltinToolDispatcher(
                    builtin_tool_registry(),
                    files,
                    process_service,
                    DurableOperationLifecycle(store),
                    workspace_id=WORKSPACE_ID,
                    clock=terminal_time,
                ).dispatch(
                    call,
                    dispatch_permit,
                    cancellation=asyncio.Event(),
                    process=process_context,
                )
                assert outcome.status is expected
                assert store.records[-1][1] == outcome.operation
        finally:
            await files.close()

    asyncio.run(scenario())


def test_mismatched_call_fails_but_cross_workspace_is_not_terminalized(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path.resolve()
        (workspace / "value.txt").write_text("value", encoding="utf-8")
        files = await WorkspaceFileService.open(workspace)
        read_call = catalog_call(
            READ_FILE_TOOL_NAME,
            {"path": "value.txt"},
            call_id="call-read-mismatch",
        )
        search_call = catalog_call(
            SEARCH_FILES_TOOL_NAME,
            {"path": ".", "query": "value"},
            call_id="call-search-mismatch",
        )
        permit, lifecycle, store = await durable_dispatch(
            read_call,
            suffix="mismatch",
        )
        try:
            dispatcher = BuiltinToolDispatcher(
                builtin_tool_registry(),
                files,
                _processes(),
                lifecycle,
                workspace_id=WORKSPACE_ID,
                clock=terminal_time,
            )
            outcome = await dispatcher.dispatch(
                search_call,
                permit,
                cancellation=asyncio.Event(),
            )
            assert outcome.status is ToolDispatchStatus.FAILED
            assert len(store.records) == 3

            wrong_workspace = "wsp_" + "9" * 32
            with pytest.raises(ToolDispatchDurabilityError):
                await BuiltinToolDispatcher(
                    builtin_tool_registry(),
                    files,
                    _processes(),
                    lifecycle,
                    workspace_id=wrong_workspace,
                    clock=terminal_time,
                ).dispatch(
                    read_call,
                    permit,
                    cancellation=asyncio.Event(),
                )
            assert len(store.records) == 3
        finally:
            await files.close()

    asyncio.run(scenario())


def test_terminal_persistence_failure_returns_no_outcome(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path.resolve()
        (workspace / "value.txt").write_text("value", encoding="utf-8")
        files = await WorkspaceFileService.open(workspace)
        call = catalog_call(
            READ_FILE_TOOL_NAME,
            {"path": "value.txt"},
            call_id="call-durability-failure",
        )
        store = RecordingStore(fail_on_write=3)
        permit, lifecycle, _ = await durable_dispatch(
            call,
            suffix="durability",
            store=store,
        )
        try:
            with pytest.raises(ToolDispatchDurabilityError):
                await BuiltinToolDispatcher(
                    builtin_tool_registry(),
                    files,
                    _processes(),
                    lifecycle,
                    workspace_id=WORKSPACE_ID,
                    clock=terminal_time,
                ).dispatch(
                    call,
                    permit,
                    cancellation=asyncio.Event(),
                )
            assert len(store.records) == 2
        finally:
            await files.close()

    asyncio.run(scenario())


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event
