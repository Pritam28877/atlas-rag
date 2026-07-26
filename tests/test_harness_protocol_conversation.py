import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ContextManifest,
    ContextOmission,
    ContextOmissionKind,
    ContextSourceKind,
    ContextSourceReference,
    DataClassification,
    ExecutionBudget,
    InlinePayload,
    ItemKind,
    ItemRecord,
    ResourceUsage,
    RetentionClass,
    ThreadRecord,
    ThreadState,
    TraceLink,
    TurnRecord,
    TurnState,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
DIGEST = "0" * 64
POLICY_VERSION = f"pol_{DIGEST}"


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


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


def usage(**overrides: int) -> ResourceUsage:
    values = {
        "steps": 1,
        "tool_calls": 0,
        "input_tokens": 100,
        "output_tokens": 20,
        "tool_output_bytes": 0,
        "duration_ms": 1_000,
        "cost_microusd": 1_000,
    }
    values.update(overrides)
    return ResourceUsage(**values)


def turn(**overrides: object) -> TurnRecord:
    values: dict[str, object] = {
        "turn_id": identifier("trn"),
        "thread_id": identifier("thr"),
        "idempotency_key": "turn-request-0001",
        "requested_agent": "coding-agent",
        "provider_policy_version": POLICY_VERSION,
        "state": TurnState.RUNNING,
        "budget": budget(),
        "usage": usage(),
        "accepted_at": NOW,
        "deadline_at": NOW + timedelta(seconds=30),
        "started_at": NOW + timedelta(seconds=1),
    }
    values.update(overrides)
    return TurnRecord.model_validate(values)


def test_thread_requires_complete_non_recursive_fork_lineage() -> None:
    thread = ThreadRecord(
        thread_id=identifier("thr"),
        workspace_id=identifier("wsp"),
        state=ThreadState.ACTIVE,
        retention_class=RetentionClass.STANDARD,
        event_sequence=0,
        created_at=NOW,
        updated_at=NOW,
    )

    assert thread.parent_thread_id is None
    with pytest.raises(ValidationError, match="fork lineage"):
        ThreadRecord.model_validate(
            {**thread.model_dump(), "parent_thread_id": identifier("thr", "1")}
        )
    with pytest.raises(ValidationError, match="own parent"):
        ThreadRecord.model_validate(
            {
                **thread.model_dump(),
                "parent_thread_id": identifier("thr"),
                "fork_event_id": identifier("evt"),
            }
        )
    with pytest.raises(ValidationError, match="cannot precede"):
        ThreadRecord.model_validate(
            {**thread.model_dump(), "updated_at": NOW - timedelta(seconds=1)}
        )


def test_turn_lifecycle_and_deadline_are_bounded() -> None:
    assert turn().state is TurnState.RUNNING

    with pytest.raises(ValidationError, match="duration budget"):
        turn(deadline_at=NOW + timedelta(seconds=31))
    with pytest.raises(ValidationError, match="started turn state"):
        turn(started_at=None)
    with pytest.raises(ValidationError, match="accepted turn cannot"):
        turn(state=TurnState.ACCEPTED)
    with pytest.raises(ValidationError, match="terminal turn requires"):
        turn(state=TurnState.COMPLETED)


def test_terminal_turn_time_and_usage_must_remain_inside_limits() -> None:
    completed_at = NOW + timedelta(seconds=10)
    completed = turn(
        state=TurnState.COMPLETED,
        completed_at=completed_at,
    )

    assert completed.completed_at == completed_at
    with pytest.raises(ValidationError, match="precedes execution"):
        turn(
            state=TurnState.FAILED,
            completed_at=NOW,
        )
    with pytest.raises(ValidationError, match="input_tokens"):
        turn(usage=usage(input_tokens=16_001))


def test_item_carries_bounded_payload_classification_and_trace() -> None:
    text = "Implement the next safe slice."
    encoded = text.encode()
    item = ItemRecord(
        item_id=identifier("itm"),
        thread_id=identifier("thr"),
        turn_id=identifier("trn"),
        ordinal=1,
        kind=ItemKind.USER,
        classification=DataClassification.INTERNAL,
        payload=InlinePayload(
            text=text,
            size_bytes=len(encoded),
            content_sha256=hashlib.sha256(encoded).hexdigest(),
        ),
        created_at=NOW,
        trace=TraceLink(
            request_id=identifier("req"),
            correlation_id=identifier("evt"),
        ),
    )

    assert item.payload.kind == "inline_text"
    assert item.redacted is False


def source(source_id: str, token_count: int) -> ContextSourceReference:
    return ContextSourceReference(
        source_id=source_id,
        kind=ContextSourceKind.INSTRUCTION,
        content_sha256=DIGEST,
        token_count=token_count,
        priority=100,
    )


def manifest(**overrides: object) -> ContextManifest:
    values: dict[str, object] = {
        "context_id": identifier("ctx"),
        "turn_id": identifier("trn"),
        "provider_policy_version": POLICY_VERSION,
        "context_window_tokens": 1_000,
        "selected_token_count": 500,
        "reserved_output_tokens": 300,
        "reserved_reasoning_tokens": 100,
        "reserved_tool_tokens": 100,
        "sources": (
            source("system:instructions", 200),
            source("thread:tail", 300),
        ),
        "omissions": (
            ContextOmission(
                source_id="document:large",
                kind=ContextOmissionKind.TOKEN_BUDGET,
                reason="The remaining source exceeds the input allocation.",
            ),
        ),
        "compiled_at": NOW,
    }
    values.update(overrides)
    return ContextManifest.model_validate(values)


def test_context_manifest_has_exact_bounded_token_accounting() -> None:
    assert manifest().selected_token_count == 500

    with pytest.raises(ValidationError, match="equal source token counts"):
        manifest(selected_token_count=499)
    with pytest.raises(ValidationError, match="exceed context window"):
        manifest(reserved_output_tokens=301)
    with pytest.raises(ValidationError, match="source IDs must be unique"):
        manifest(
            sources=(
                source("thread:tail", 200),
                source("thread:tail", 300),
            )
        )
    with pytest.raises(ValidationError, match="omission IDs must be unique"):
        omission = ContextOmission(
            source_id="document:large",
            kind=ContextOmissionKind.STALE,
            reason="The source is stale.",
        )
        manifest(omissions=(omission, omission))


def test_context_source_cannot_point_to_two_storage_records() -> None:
    with pytest.raises(ValidationError, match="artifact and event together"):
        ContextSourceReference(
            source_id="artifact:result",
            kind=ContextSourceKind.TASK_ARTIFACT,
            content_sha256=DIGEST,
            token_count=10,
            priority=1,
            artifact_id=identifier("art"),
            event_id=identifier("evt"),
        )
