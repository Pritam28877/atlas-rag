import hashlib

import pytest
from pydantic import TypeAdapter, ValidationError

from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderError,
    ProviderFailureClass,
    ProviderFinishReason,
    ProviderStreamBatch,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderToolCall,
    ProviderUsage,
)


def tool_call(**overrides: object) -> ProviderToolCall:
    arguments_json = '{"path":"README.md"}'
    values: dict[str, object] = {
        "sequence": 2,
        "call_id": "call_00000001",
        "tool_name": "workspace.read_file",
        "arguments_json": arguments_json,
        "arguments_sha256": hashlib.sha256(arguments_json.encode()).hexdigest(),
    }
    values.update(overrides)
    return ProviderToolCall.model_validate(values)


def test_provider_stream_batch_is_discriminated_and_contiguous() -> None:
    batch = ProviderStreamBatch(
        events=(
            ProviderTextDelta(sequence=1, text="hello"),
            tool_call(),
            ProviderUsage(
                sequence=3,
                usage=ProviderTokenUsage(
                    input_tokens=10,
                    cached_input_tokens=2,
                    output_tokens=3,
                    reasoning_tokens=1,
                    cost_microusd=12,
                ),
            ),
            ProviderCompleted(
                sequence=4,
                finish_reason=ProviderFinishReason.TOOL_CALLS,
            ),
        )
    )
    reparsed = ProviderStreamBatch.model_validate_json(batch.model_dump_json())

    assert reparsed == batch
    assert isinstance(reparsed.events[1], ProviderToolCall)


def test_tool_call_requires_canonical_hashed_object_arguments() -> None:
    with pytest.raises(ValueError, match="canonical"):
        tool_call(arguments_json='{ "path": "README.md" }')
    with pytest.raises(ValueError, match="hash mismatch"):
        tool_call(arguments_sha256="f" * 64)
    with pytest.raises(ValueError, match="JSON object"):
        tool_call(
            arguments_json="[]",
            arguments_sha256=hashlib.sha256(b"[]").hexdigest(),
        )


def test_provider_call_id_preserves_safe_opaque_vendor_value() -> None:
    provider_call = tool_call(call_id="tooluse_Az.09:vendor")

    assert provider_call.call_id == "tooluse_Az.09:vendor"
    with pytest.raises(ValueError):
        tool_call(call_id="call_contains/slash")
    with pytest.raises(ValueError):
        tool_call(call_id="call_contains space")


def test_usage_and_error_retry_classification_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="cached input"):
        ProviderTokenUsage(
            input_tokens=1,
            cached_input_tokens=2,
            output_tokens=0,
            reasoning_tokens=0,
            cost_microusd=0,
        )
    with pytest.raises(ValueError, match="retry classification"):
        ProviderError(
            sequence=1,
            failure_class=ProviderFailureClass.AUTHENTICATION,
            retry_allowed=True,
            reason="Authentication failed.",
        )


def test_unknown_kind_and_sequence_gap_are_rejected() -> None:
    adapter: TypeAdapter[ProviderStreamEvent] = TypeAdapter(ProviderStreamEvent)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {"kind": "unknown", "sequence": 1, "text": "no"}
        )
    with pytest.raises(ValueError, match="contiguous"):
        ProviderStreamBatch(
            events=(
                ProviderTextDelta(sequence=1, text="first"),
                ProviderCompleted(
                    sequence=3,
                    finish_reason=ProviderFinishReason.STOP,
                ),
            )
        )
