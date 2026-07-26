import hashlib

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ApprovalRespondCommand,
    ApprovalState,
    CommandEnvelope,
    DataClassification,
    ExecutionBudget,
    InlinePayload,
    PageRequest,
    RetentionClass,
    ThreadArchiveCommand,
    ThreadCreateCommand,
    ThreadForkCommand,
    ThreadListCommand,
    ThreadResumeCommand,
    ThreadState,
    TurnCancelCommand,
    TurnCompactCommand,
    TurnStartCommand,
    TurnSteerCommand,
    WorkspaceAccessMode,
    WorkspaceCloseCommand,
    WorkspaceOpenCommand,
)

DIGEST = "0" * 64
IDEMPOTENCY_KEY = "command-request-0001"


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


def payload() -> InlinePayload:
    text = "bounded client content"
    encoded = text.encode()
    return InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )


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


def session_commands() -> tuple[object, ...]:
    return (
        WorkspaceOpenCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            access_mode=WorkspaceAccessMode.READ_WRITE,
            repository_fingerprint_sha256=DIGEST,
        ),
        WorkspaceCloseCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            reason="Client is releasing the workspace.",
        ),
        ThreadCreateCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            retention_class=RetentionClass.STANDARD,
        ),
        ThreadResumeCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            thread_id=identifier("thr"),
        ),
        ThreadForkCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            source_thread_id=identifier("thr"),
            fork_event_id=identifier("evt"),
            retention_class=RetentionClass.STANDARD,
        ),
        ThreadArchiveCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            thread_id=identifier("thr"),
            reason="The work is complete.",
        ),
        ThreadListCommand(
            page=PageRequest(limit=50),
            states=(ThreadState.ACTIVE, ThreadState.ARCHIVED),
        ),
        TurnStartCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            thread_id=identifier("thr"),
            requested_agent="coding-agent",
            budget=budget(),
            classification=DataClassification.INTERNAL,
            initial_payload=payload(),
        ),
        TurnSteerCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            turn_id=identifier("trn"),
            classification=DataClassification.INTERNAL,
            payload=payload(),
        ),
        TurnCancelCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            turn_id=identifier("trn"),
            reason="The user cancelled the turn.",
        ),
        TurnCompactCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            turn_id=identifier("trn"),
            target_input_tokens=8_000,
            reason="Reserve space for the remaining task.",
        ),
        ApprovalRespondCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            approval_id=identifier("apr"),
            request_sha256=DIGEST,
            decision=ApprovalState.APPROVED,
            reason="The exact workspace write is approved.",
        ),
    )


def envelope(command: object, expected_sequence: int | None = 7) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "schema_version": "1.0",
            "request_id": identifier("req"),
            "client_id": identifier("cli"),
            "workspace_id": identifier("wsp"),
            "expected_sequence": expected_sequence,
            "command": command,
        }
    )


def test_every_session_command_round_trips_through_discriminated_json() -> None:
    commands = session_commands()
    kinds = set()

    for command in commands:
        is_query = isinstance(command, ThreadListCommand)
        record = envelope(
            command,
            expected_sequence=None if is_query else 7,
        )
        reparsed = CommandEnvelope.model_validate_json(record.model_dump_json())
        kinds.add(reparsed.command.kind)
        assert type(reparsed.command) is type(command)

    assert len(kinds) == len(commands)


def test_envelope_rejects_authority_claims_and_unknown_commands() -> None:
    record = envelope(session_commands()[0])
    values = record.model_dump()

    with pytest.raises(ValidationError, match="extra_forbidden"):
        CommandEnvelope.model_validate(
            {**values, "principal_id": identifier("prn")}
        )
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        CommandEnvelope.model_validate(
            {
                **values,
                "command": {
                    "kind": "workspace.become_admin",
                    "idempotency_key": IDEMPOTENCY_KEY,
                },
            }
        )


def test_mutation_requires_idempotency_and_rejects_type_coercion() -> None:
    record = envelope(session_commands()[0])
    command_values = record.command.model_dump()
    command_values.pop("idempotency_key")

    with pytest.raises(ValidationError, match="idempotency_key"):
        CommandEnvelope.model_validate(
            {**record.model_dump(), "command": command_values}
        )
    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {**record.model_dump(), "expected_sequence": "7"}
        )


def test_query_forbids_mutation_metadata_and_sequence_precondition() -> None:
    command = ThreadListCommand(
        states=(ThreadState.ACTIVE,),
    )

    assert envelope(command, expected_sequence=None).command == command
    with pytest.raises(ValidationError, match="query command"):
        envelope(command, expected_sequence=0)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ThreadListCommand.model_validate(
            {
                **command.model_dump(),
                "idempotency_key": IDEMPOTENCY_KEY,
            }
        )
    with pytest.raises(ValidationError):
        ThreadListCommand(
            page=PageRequest(limit=200),
            states=(ThreadState.ACTIVE, ThreadState.ACTIVE),
        )


def test_approval_response_accepts_only_final_human_decisions() -> None:
    with pytest.raises(ValidationError, match="approved or denied"):
        ApprovalRespondCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            approval_id=identifier("apr"),
            request_sha256=DIGEST,
            decision=ApprovalState.REQUESTED,
            reason="Invalid response state.",
        )
