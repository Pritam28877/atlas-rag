import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.services.harness.protocol import (
    BlobPayload,
    ExecutionBudget,
    InlinePayload,
    InvalidTransitionError,
    OperationState,
    PageInfo,
    PageRequest,
    PrincipalId,
    ResourceUsage,
    StrictProtocolModel,
    TaskState,
    TurnState,
    UtcTimestamp,
    WorkspaceId,
    require_operation_transition,
    require_task_transition,
    require_turn_transition,
)


def identifier(prefix: str) -> str:
    return f"{prefix}_0123456789abcdef0123456789abcdef"


def budget() -> ExecutionBudget:
    return ExecutionBudget(
        max_steps=8,
        max_tool_calls=4,
        max_input_tokens=16_000,
        max_output_tokens=4_000,
        max_tool_output_bytes=1024,
        max_duration_ms=30_000,
        max_cost_microusd=250_000,
    )


def test_identifier_prefix_and_casing_are_enforced() -> None:
    principal_adapter = TypeAdapter(PrincipalId)
    workspace_adapter = TypeAdapter(WorkspaceId)

    assert principal_adapter.validate_python(identifier("prn")) == identifier("prn")
    with pytest.raises(ValidationError):
        principal_adapter.validate_python(identifier("wsp"))
    with pytest.raises(ValidationError):
        workspace_adapter.validate_python(identifier("wsp").upper())


def test_page_request_rejects_coercion_and_unbounded_limit() -> None:
    assert PageRequest().limit == 50

    with pytest.raises(ValidationError):
        PageRequest(limit="50")
    with pytest.raises(ValidationError):
        PageRequest(limit=201)


def test_page_info_requires_cursor_exactly_when_more_results_exist() -> None:
    cursor = "cursordata_0123456789"

    assert PageInfo(next_cursor=cursor, has_more=True).next_cursor == cursor
    with pytest.raises(ValidationError, match="next_cursor"):
        PageInfo(next_cursor=None, has_more=True)
    with pytest.raises(ValidationError, match="next_cursor"):
        PageInfo(next_cursor=cursor, has_more=False)


def test_budget_is_strict_and_usage_reports_every_exceeded_dimension() -> None:
    usage = ResourceUsage(
        steps=9,
        tool_calls=4,
        input_tokens=16_001,
        output_tokens=4_000,
        tool_output_bytes=1025,
        duration_ms=30_001,
        cost_microusd=250_001,
    )

    assert usage.exceeds(budget()) == (
        "steps",
        "input_tokens",
        "tool_output_bytes",
        "duration_ms",
        "cost_microusd",
    )
    with pytest.raises(ValidationError):
        ExecutionBudget.model_validate({**budget().model_dump(), "max_steps": "8"})
    with pytest.raises(ValidationError):
        ExecutionBudget.model_validate(
            {**budget().model_dump(), "max_duration_ms": 3_600_001}
        )


def test_inline_payload_recomputes_utf8_size_and_hash() -> None:
    text = "bounded π"
    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    payload = InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=digest,
    )

    assert payload.content_sha256 == digest
    with pytest.raises(ValidationError, match="size"):
        InlinePayload(
            text=text,
            size_bytes=len(encoded) - 1,
            content_sha256=digest,
        )
    with pytest.raises(ValidationError, match="hash"):
        InlinePayload(
            text=text,
            size_bytes=len(encoded),
            content_sha256="0" * 64,
        )


def test_payloads_reject_unknown_fields_and_oversized_blob() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        InlinePayload.model_validate(
            {
                "text": "safe",
                "size_bytes": 4,
                "content_sha256": hashlib.sha256(b"safe").hexdigest(),
                "unexpected": "hostile",
            }
        )
    with pytest.raises(ValidationError):
        BlobPayload(
            artifact_id=identifier("art"),
            media_type="application/octet-stream",
            size_bytes=4 * 1024 * 1024 * 1024 + 1,
            content_sha256="0" * 64,
        )


class TimestampRecord(StrictProtocolModel):
    created_at: UtcTimestamp


def test_timestamp_requires_explicit_utc_without_normalization() -> None:
    timestamp = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    assert TimestampRecord(created_at=timestamp).created_at == timestamp
    with pytest.raises(ValidationError, match="UTC"):
        TimestampRecord(created_at=timestamp.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="UTC"):
        TimestampRecord(
            created_at=timestamp.astimezone(timezone(timedelta(hours=5, minutes=30)))
        )


def test_terminal_and_out_of_order_transitions_fail_closed() -> None:
    require_turn_transition(TurnState.ACCEPTED, TurnState.RUNNING)
    require_operation_transition(
        OperationState.DISPATCHED,
        OperationState.AMBIGUOUS,
    )
    require_task_transition(TaskState.BLOCKED, TaskState.READY)

    with pytest.raises(InvalidTransitionError):
        require_turn_transition(TurnState.COMPLETED, TurnState.RUNNING)
    with pytest.raises(InvalidTransitionError):
        require_operation_transition(
            OperationState.PREPARED,
            OperationState.COMPLETED,
        )
    with pytest.raises(InvalidTransitionError):
        require_task_transition(TaskState.PENDING, TaskState.COMPLETED)
