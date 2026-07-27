"""Shared durable background-job test fixtures."""

import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path

from app.services.harness.journal import SQLiteRecoveryStore
from app.services.harness.protocol import (
    IdempotencyClass,
    OperationRecord,
    OperationState,
)
from app.services.harness.protocol.background_jobs import (
    BackgroundJobArtifact,
    BackgroundJobRecord,
    BackgroundJobState,
)
from app.services.harness.tools.operation_lifecycle import (
    DurableOperationLifecycle,
)
from tests.harness.operations.fixtures import (
    ARGS_SHA256,
    NOW,
    OPERATION_ID,
    WORKSPACE_ID,
    fence,
    request,
)

OWNER_ID = "prn_" + "7" * 32
OTHER_OWNER_ID = "prn_" + "8" * 32
EXECUTION_OWNER_ID = "jow_" + "9" * 32


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def queued_job() -> BackgroundJobRecord:
    return BackgroundJobRecord(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        owner_principal_id=OWNER_ID,
        call_id="background-call-1",
        requested_name="workspace.write_file",
        tool_name="workspace.write_file",
        tool_version="1.0.0",
        capability="filesystem.write",
        idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
        descriptor_sha256="a" * 64,
        arguments_json=(
            '{"content_sha256":"'
            + "3" * 64
            + '","path":"src/app.py"}'
        ),
        args_sha256=ARGS_SHA256,
        state=BackgroundJobState.QUEUED,
        created_at=NOW + timedelta(seconds=2),
        updated_at=NOW + timedelta(seconds=2),
        queue_expires_at=NOW + timedelta(hours=1),
    )


def transition(
    job: BackgroundJobRecord,
    **changes: object,
) -> BackgroundJobRecord:
    values = job.model_dump(mode="python")
    values.update(changes)
    return BackgroundJobRecord.model_validate(values)


def result_artifact() -> BackgroundJobArtifact:
    return BackgroundJobArtifact(
        artifact_id="art_" + "a" * 32,
        media_type="application/json",
        size_bytes=24,
        content_sha256="b" * 64,
    )


async def save_dispatched_operation(path: Path) -> OperationRecord:
    recovery_store = await SQLiteRecoveryStore.open(path)
    lifecycle = DurableOperationLifecycle(recovery_store)
    prepared = await lifecycle.prepare(request(), fence(), prepared_at=NOW)
    permit = await lifecycle.dispatch(
        prepared,
        fence(),
        dispatched_at=NOW + timedelta(seconds=1),
    )
    await recovery_store.close()
    return permit.operation


def terminal_operation(
    operation: OperationRecord,
    state: OperationState,
    *,
    result_content: bytes | None = None,
    status_reason: str | None = None,
) -> OperationRecord:
    values = operation.model_dump(mode="python")
    values.update(
        {
            "state": state,
            "terminal_at": NOW + timedelta(seconds=2),
            "result_sha256": (
                hashlib.sha256(result_content).hexdigest()
                if result_content is not None
                else None
            ),
            "status_reason": status_reason,
        }
    )
    return OperationRecord.model_validate(values)


async def persist_terminal_operation(
    database: Path,
    operation: OperationRecord,
) -> None:
    store = await SQLiteRecoveryStore.open(database)
    await store.save_operation(
        WORKSPACE_ID,
        operation,
        updated_at=NOW + timedelta(seconds=2),
    )
    await store.close()
