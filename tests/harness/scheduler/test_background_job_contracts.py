"""Background-job ownership, lifecycle, and bound contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import IdempotencyClass
from app.services.harness.scheduler import (
    BackgroundJobArtifact,
    BackgroundJobConfiguration,
    BackgroundJobRecord,
    BackgroundJobState,
    require_background_job_transition,
)
from tests.harness.operations.fixtures import ARGS_SHA256

NOW = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
SHA256 = "a" * 64


def job_record(
    state: BackgroundJobState,
    **changes: object,
) -> BackgroundJobRecord:
    values: dict[str, object] = {
        "workspace_id": f"wsp_{'1' * 32}",
        "operation_id": f"opn_{'2' * 32}",
        "owner_principal_id": f"prn_{'3' * 32}",
        "call_id": "call-1",
        "requested_name": "workspace.read",
        "tool_name": "workspace.read",
        "tool_version": "1.0.0",
        "capability": "filesystem.write",
        "idempotency_class": IdempotencyClass.NON_IDEMPOTENT,
        "descriptor_sha256": SHA256,
        "arguments_json": (
            '{"content_sha256":"'
            + "3" * 64
            + '","path":"src/app.py"}'
        ),
        "args_sha256": ARGS_SHA256,
        "state": state,
        "execution_generation": 0,
        "created_at": NOW,
        "updated_at": NOW,
        "queue_expires_at": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return BackgroundJobRecord.model_validate(values)


def result_artifact() -> BackgroundJobArtifact:
    return BackgroundJobArtifact(
        artifact_id=f"art_{'4' * 32}",
        media_type="application/json",
        size_bytes=24,
        content_sha256="c" * 64,
    )


def test_configuration_enforces_global_bounds() -> None:
    configuration = BackgroundJobConfiguration()

    assert configuration.maximum_concurrent_jobs == 4
    with pytest.raises(ValidationError):
        BackgroundJobConfiguration(maximum_concurrent_jobs=257)
    with pytest.raises(ValidationError):
        BackgroundJobConfiguration(retention_ttl_seconds=2_592_001)
    with pytest.raises(ValidationError, match="aggregate output"):
        BackgroundJobConfiguration(
            maximum_concurrent_jobs=256,
            maximum_log_bytes=1024 * 1024,
            maximum_result_bytes=1024 * 1024,
        )


def test_queued_job_has_no_runtime_owner() -> None:
    record = job_record(BackgroundJobState.QUEUED)

    assert record.execution_owner_id is None
    assert record.terminal_at is None


def test_active_job_requires_fenced_owner_and_unexpired_lease() -> None:
    with pytest.raises(ValidationError, match="execution owner"):
        job_record(
            BackgroundJobState.RUNNING,
            started_at=NOW,
            execution_generation=1,
        )

    record = job_record(
        BackgroundJobState.RUNNING,
        started_at=NOW,
        execution_generation=1,
        execution_owner_id=f"jow_{'5' * 32}",
        execution_lease_expires_at=NOW + timedelta(seconds=30),
    )

    assert record.execution_generation == 1


def test_completed_job_requires_bounded_result_and_retention() -> None:
    terminal_at = NOW + timedelta(seconds=5)
    record = job_record(
        BackgroundJobState.COMPLETED,
        execution_generation=1,
        started_at=NOW,
        updated_at=terminal_at,
        terminal_at=terminal_at,
        retention_expires_at=terminal_at + timedelta(days=1),
        result_artifact=result_artifact(),
    )

    assert record.result_artifact == result_artifact()
    with pytest.raises(ValidationError, match="result artifact"):
        job_record(
            BackgroundJobState.COMPLETED,
            execution_generation=1,
            started_at=NOW,
            updated_at=terminal_at,
            terminal_at=terminal_at,
            retention_expires_at=terminal_at + timedelta(days=1),
        )


def test_started_job_can_finish_after_queue_ttl() -> None:
    terminal_at = NOW + timedelta(hours=2)

    record = job_record(
        BackgroundJobState.COMPLETED,
        execution_generation=1,
        started_at=NOW,
        updated_at=terminal_at,
        terminal_at=terminal_at,
        retention_expires_at=terminal_at + timedelta(days=1),
        result_artifact=result_artifact(),
    )

    assert record.terminal_at == terminal_at


def test_queued_job_can_be_cancelled_without_runtime_owner() -> None:
    terminal_at = NOW + timedelta(seconds=1)

    record = job_record(
        BackgroundJobState.CANCELLED,
        updated_at=terminal_at,
        cancellation_requested_at=terminal_at,
        terminal_at=terminal_at,
        retention_expires_at=terminal_at + timedelta(days=1),
        status_reason="cancelled before execution",
    )

    assert record.execution_generation == 0


def test_cancellation_requires_request_evidence() -> None:
    with pytest.raises(ValidationError, match="cancellation evidence"):
        job_record(
            BackgroundJobState.CANCELLING,
            execution_generation=1,
            execution_owner_id=f"jow_{'5' * 32}",
            execution_lease_expires_at=NOW + timedelta(seconds=30),
            started_at=NOW,
        )


def test_transitions_reject_terminal_replay() -> None:
    require_background_job_transition(
        BackgroundJobState.QUEUED,
        BackgroundJobState.RUNNING,
    )

    with pytest.raises(ValueError, match="completed->running"):
        require_background_job_transition(
            BackgroundJobState.COMPLETED,
            BackgroundJobState.RUNNING,
        )
