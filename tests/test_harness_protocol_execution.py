import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ApprovalRecord,
    ApprovalScope,
    ApprovalState,
    EventActorKind,
    EventRecord,
    IdempotencyClass,
    InlinePayload,
    OperationLimits,
    OperationRecord,
    OperationState,
    TraceLink,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
DIGEST = "0" * 64
POLICY_VERSION = f"pol_{DIGEST}"


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


def inline_payload() -> InlinePayload:
    text = "bounded event metadata"
    encoded = text.encode()
    return InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def trace() -> TraceLink:
    return TraceLink(
        request_id=identifier("req"),
        correlation_id=identifier("evt", "1"),
    )


def event(**overrides: object) -> EventRecord:
    values: dict[str, object] = {
        "event_id": identifier("evt"),
        "event_type": "Turn.Accepted",
        "schema_version": "1.0",
        "aggregate_id": identifier("trn"),
        "aggregate_sequence": 1,
        "actor_kind": EventActorKind.AUTHENTICATED,
        "actor_principal_id": identifier("prn"),
        "grant_id": identifier("grt"),
        "policy_decision_id": identifier("dcs"),
        "occurred_at": NOW,
        "trace": trace(),
        "payload": inline_payload(),
    }
    values.update(overrides)
    return EventRecord.model_validate(values)


def test_event_requires_complete_actor_authority_evidence() -> None:
    assert event().aggregate_sequence == 1

    with pytest.raises(ValidationError, match="must be paired"):
        event(policy_decision_id=None)
    with pytest.raises(ValidationError, match="requires authorization"):
        event(grant_id=None, policy_decision_id=None)
    with pytest.raises(ValidationError, match="cannot claim"):
        event(actor_kind=EventActorKind.SYSTEM)

    system_event = event(
        actor_kind=EventActorKind.SYSTEM,
        grant_id=None,
        policy_decision_id=None,
    )
    assert system_event.actor_kind is EventActorKind.SYSTEM


def operation_limits() -> OperationLimits:
    return OperationLimits(
        max_duration_ms=30_000,
        max_cpu_ms=10_000,
        max_memory_bytes=256 * 1024 * 1024,
        max_output_bytes=1024,
        max_processes=8,
    )


def operation(**overrides: object) -> OperationRecord:
    values: dict[str, object] = {
        "operation_id": identifier("opn"),
        "turn_id": identifier("trn"),
        "idempotency_key": "operation-request-0001",
        "idempotency_class": IdempotencyClass.NON_IDEMPOTENT,
        "attempt": 1,
        "lease_fencing_token": 42,
        "tool_name": "workspace.write_file",
        "tool_version": "1.0.0",
        "args_sha256": DIGEST,
        "capability": "filesystem.write",
        "policy_decision_id": identifier("dcs"),
        "approval_id": identifier("apr"),
        "state": OperationState.PREPARED,
        "limits": operation_limits(),
        "prepared_at": NOW,
    }
    values.update(overrides)
    return OperationRecord.model_validate(values)


def test_operation_lifecycle_requires_dispatch_and_fencing_evidence() -> None:
    assert operation().lease_fencing_token == 42

    with pytest.raises(ValidationError, match="dispatched_at"):
        operation(state=OperationState.DISPATCHED)
    with pytest.raises(ValidationError, match="cannot precede preparation"):
        operation(
            state=OperationState.DISPATCHED,
            dispatched_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="active operation"):
        operation(status_reason="Unexpected stale error.")


def test_operation_preserves_ambiguous_and_terminal_outcomes() -> None:
    dispatched_at = NOW + timedelta(seconds=1)
    ambiguous = operation(
        state=OperationState.AMBIGUOUS,
        dispatched_at=dispatched_at,
        ambiguous_at=NOW + timedelta(seconds=2),
        status_reason="The executor disconnected after dispatch.",
    )
    assert ambiguous.state is OperationState.AMBIGUOUS

    completed = operation(
        state=OperationState.COMPLETED,
        dispatched_at=dispatched_at,
        terminal_at=NOW + timedelta(seconds=3),
        result_sha256="1" * 64,
    )
    assert completed.result_sha256 == "1" * 64

    with pytest.raises(ValidationError, match="status reason"):
        operation(
            state=OperationState.AMBIGUOUS,
            dispatched_at=dispatched_at,
            ambiguous_at=NOW + timedelta(seconds=2),
        )
    with pytest.raises(ValidationError, match="result hash"):
        operation(
            state=OperationState.COMPLETED,
            dispatched_at=dispatched_at,
            terminal_at=NOW + timedelta(seconds=3),
        )
    with pytest.raises(ValidationError, match="status reason"):
        operation(
            state=OperationState.FAILED,
            dispatched_at=dispatched_at,
            terminal_at=NOW + timedelta(seconds=3),
        )


def approval(**overrides: object) -> ApprovalRecord:
    values: dict[str, object] = {
        "approval_id": identifier("apr"),
        "operation_id": identifier("opn"),
        "request_sha256": DIGEST,
        "capability": "filesystem.write",
        "scope": ApprovalScope.ONCE,
        "state": ApprovalState.REQUESTED,
        "policy_version": POLICY_VERSION,
        "matched_rule_ids": ("ask-workspace-write",),
        "requested_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "reason": "Workspace write needs explicit approval.",
    }
    values.update(overrides)
    return ApprovalRecord.model_validate(values)


def test_approval_is_hash_bound_expiring_and_append_only() -> None:
    assert approval().request_sha256 == DIGEST
    decided_at = NOW + timedelta(minutes=1)
    approved = approval(
        state=ApprovalState.APPROVED,
        decided_at=decided_at,
        decided_by_principal_id=identifier("prn"),
    )
    assert approved.decided_at == decided_at

    with pytest.raises(ValidationError, match="unique and sorted"):
        approval(matched_rule_ids=("z-rule", "a-rule"))
    with pytest.raises(ValidationError, match="requires decided_at"):
        approval(state=ApprovalState.APPROVED)
    with pytest.raises(ValidationError, match="deciding principal"):
        approval(
            state=ApprovalState.DENIED,
            decided_at=decided_at,
        )
    with pytest.raises(ValidationError, match="validity window"):
        approval(
            state=ApprovalState.APPROVED,
            decided_at=NOW + timedelta(minutes=11),
            decided_by_principal_id=identifier("prn"),
        )
