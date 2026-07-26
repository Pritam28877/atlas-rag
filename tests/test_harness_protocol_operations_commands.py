from typing import cast

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ArtifactFetchCommand,
    CapabilityCatalog,
    CapabilityListCommand,
    CommandEnvelope,
    DataClassification,
    EvaluationStartCommand,
    EvaluationStatusCommand,
    EventAcknowledgeCommand,
    EventSubscribeCommand,
    ExecutionBudget,
    MutatingCommand,
    PageRequest,
    TaskCancelCommand,
    TaskInspectCommand,
    TaskRetryCommand,
)

DIGEST = "0" * 64
IDEMPOTENCY_KEY = "command-request-0001"


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


def budget() -> ExecutionBudget:
    return ExecutionBudget(
        max_steps=64,
        max_tool_calls=16,
        max_input_tokens=64_000,
        max_output_tokens=16_000,
        max_tool_output_bytes=4 * 1024 * 1024,
        max_duration_ms=300_000,
        max_cost_microusd=5_000_000,
    )


def operations_commands() -> tuple[object, ...]:
    return (
        TaskInspectCommand(task_id=identifier("tsk")),
        TaskCancelCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            task_id=identifier("tsk"),
            reason="The parent task was cancelled.",
        ),
        TaskRetryCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            task_id=identifier("tsk"),
            failed_attempt=1,
            task_spec_sha256=DIGEST,
            reason="The classified transient failure is retryable.",
        ),
        CapabilityListCommand(
            catalog=CapabilityCatalog.MODEL,
            required_capabilities=("reasoning", "tools"),
            page=PageRequest(limit=50),
        ),
        EventSubscribeCommand(
            after_sequence=42,
            event_types=("Artifact.Durable", "Turn.Completed"),
        ),
        EventAcknowledgeCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            subscription_id=identifier("sub"),
            through_sequence=100,
        ),
        ArtifactFetchCommand(
            artifact_id=identifier("art"),
            expected_content_sha256=DIGEST,
            offset_bytes=1024,
            length_bytes=4096,
        ),
        EvaluationStartCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            fixture_sha256s=("1" * 64, "2" * 64),
            configuration_sha256="3" * 64,
            classification=DataClassification.INTERNAL,
            budget=budget(),
            max_parallelism=4,
        ),
        EvaluationStatusCommand(evaluation_id=identifier("evl")),
    )


def envelope(command: object) -> CommandEnvelope:
    expected_sequence = 7 if isinstance(command, MutatingCommand) else None
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


def test_every_operations_command_round_trips_through_shared_envelope() -> None:
    commands = operations_commands()
    kinds = set()

    for command in commands:
        record = envelope(command)
        reparsed = CommandEnvelope.model_validate_json(record.model_dump_json())
        kinds.add(reparsed.command.kind)
        assert type(reparsed.command) is type(command)

    assert len(kinds) == len(commands)


def test_task_retry_is_bound_to_failed_attempt_and_task_specification() -> None:
    with pytest.raises(ValidationError):
        TaskRetryCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            task_id=identifier("tsk"),
            failed_attempt=0,
            task_spec_sha256=DIGEST,
            reason="Invalid attempt.",
        )
    with pytest.raises(ValidationError):
        TaskRetryCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            task_id=identifier("tsk"),
            failed_attempt=1,
            task_spec_sha256="not-a-digest",
            reason="Invalid task specification.",
        )


def test_catalog_and_subscription_filters_are_canonical_and_bounded() -> None:
    with pytest.raises(ValidationError, match="unique and sorted"):
        CapabilityListCommand(
            catalog=CapabilityCatalog.TOOL,
            required_capabilities=("tools", "reasoning"),
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        EventSubscribeCommand(
            after_sequence=0,
            event_types=("Turn.Completed", "Artifact.Durable"),
        )
    with pytest.raises(ValidationError):
        EventSubscribeCommand(
            after_sequence=0,
            max_batch_size=257,
        )


def test_event_acknowledgement_is_a_replay_safe_mutation() -> None:
    command = cast(EventAcknowledgeCommand, operations_commands()[5])
    record = envelope(command)

    assert record.expected_sequence == 7
    command_values = command.model_dump()
    command_values.pop("idempotency_key")
    with pytest.raises(ValidationError, match="idempotency_key"):
        CommandEnvelope.model_validate(
            {
                **record.model_dump(),
                "command": command_values,
            }
        )


def test_artifact_fetch_range_is_hash_bound_and_cannot_overflow() -> None:
    maximum_size = 4 * 1024 * 1024 * 1024

    with pytest.raises(ValidationError, match="exceeds maximum"):
        ArtifactFetchCommand(
            artifact_id=identifier("art"),
            expected_content_sha256=DIGEST,
            offset_bytes=maximum_size - 1,
            length_bytes=2,
        )
    with pytest.raises(ValidationError):
        ArtifactFetchCommand(
            artifact_id=identifier("art"),
            expected_content_sha256="f" * 63,
        )


def test_evaluation_start_has_sorted_bounded_fixtures_and_parallelism() -> None:
    with pytest.raises(ValidationError, match="unique and sorted"):
        EvaluationStartCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            fixture_sha256s=("2" * 64, "1" * 64),
            configuration_sha256=DIGEST,
            classification=DataClassification.INTERNAL,
            budget=budget(),
        )
    with pytest.raises(ValidationError):
        EvaluationStartCommand(
            idempotency_key=IDEMPOTENCY_KEY,
            fixture_sha256s=("1" * 64,),
            configuration_sha256=DIGEST,
            classification=DataClassification.INTERNAL,
            budget=budget(),
            max_parallelism=65,
        )
