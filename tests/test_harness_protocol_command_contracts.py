from typing import Any, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from app.services.harness.protocol import (
    COMMAND_CONTRACT_BY_KIND,
    COMMAND_CONTRACTS,
    CanonicalEventType,
    CommandContract,
    CommandEnvelope,
    CommandKind,
    EventType,
    IdempotencyRequirement,
    SequencePrecondition,
    command_contract,
)


def command_schemas() -> dict[str, dict[str, Any]]:
    schema = CommandEnvelope.model_json_schema()
    definitions = cast(dict[str, dict[str, Any]], schema["$defs"])
    records: dict[str, dict[str, Any]] = {}
    command_kinds = {command_kind.value for command_kind in CommandKind}
    for definition in definitions.values():
        properties = definition.get("properties", {})
        kind_schema = properties.get("kind", {})
        kind = kind_schema.get("const")
        if isinstance(kind, str) and kind in command_kinds:
            records[kind] = definition
    return records


def test_contract_registry_exactly_covers_envelope_discriminator() -> None:
    schema = CommandEnvelope.model_json_schema()
    command_schema = cast(dict[str, Any], schema["properties"]["command"])
    discriminator = cast(dict[str, Any], command_schema["discriminator"])
    mapping = cast(dict[str, str], discriminator["mapping"])

    schema_kinds = set(mapping)
    enum_kinds = {kind.value for kind in CommandKind}
    contract_kinds = {contract.kind.value for contract in COMMAND_CONTRACTS}
    assert schema_kinds == enum_kinds == contract_kinds
    assert len(COMMAND_CONTRACTS) == len(COMMAND_CONTRACT_BY_KIND)


def test_contract_idempotency_matches_each_command_schema() -> None:
    schemas = command_schemas()

    for kind, schema in schemas.items():
        required_fields = set(schema.get("required", ()))
        has_required_replay_key = "idempotency_key" in required_fields
        contract = command_contract(CommandKind(kind))
        if has_required_replay_key:
            assert contract.idempotency is IdempotencyRequirement.REQUIRED
            assert (
                contract.sequence_precondition
                is SequencePrecondition.OPTIONAL
            )
        else:
            assert contract.idempotency is IdempotencyRequirement.FORBIDDEN
            assert (
                contract.sequence_precondition
                is SequencePrecondition.FORBIDDEN
            )


def test_every_contract_documents_authorization_response_and_events() -> None:
    event_adapter = TypeAdapter(EventType)

    for contract in COMMAND_CONTRACTS:
        assert contract.authorization_capability
        assert contract.response.value
        for event_type in contract.emitted_events:
            assert event_adapter.validate_python(event_type.value) == event_type.value


def test_sensitive_commands_require_fresh_authorization() -> None:
    reauthorized = {
        contract.kind
        for contract in COMMAND_CONTRACTS
        if contract.reauthorize_on_execution
    }

    assert reauthorized == {
        CommandKind.APPROVAL_RESPOND,
        CommandKind.EVENT_SUBSCRIBE,
        CommandKind.EVENT_ACKNOWLEDGE,
    }


def test_canonical_events_include_ambiguity_resync_and_recovery() -> None:
    values = {event_type.value for event_type in CanonicalEventType}

    assert len(values) == len(CanonicalEventType)
    assert CanonicalEventType.OPERATION_AMBIGUOUS.value in values
    assert CanonicalEventType.EVENT_RESYNC_REQUIRED.value in values
    assert CanonicalEventType.RECOVERY_RECONCILED.value in values
    assert CanonicalEventType.PROVIDER_USAGE_RECORDED.value in values
    assert CanonicalEventType.POLICY_DECISION_RECORDED.value in values
    assert CanonicalEventType.CONTEXT_COMPILED.value in values


def test_contracts_and_registry_are_immutable() -> None:
    contract = command_contract(CommandKind.TURN_START)

    with pytest.raises(ValidationError, match="frozen"):
        contract.reauthorize_on_execution = True
    mutable_view = cast(dict[CommandKind, CommandContract], COMMAND_CONTRACT_BY_KIND)
    with pytest.raises(TypeError):
        mutable_view[CommandKind.TURN_START] = contract
