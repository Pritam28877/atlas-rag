import hashlib
import json
from pathlib import Path

import pytest

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderContextPlan,
    ProviderError,
    ProviderFailureClass,
    ProviderFinishReason,
    ProviderMessage,
    ProviderMessageRole,
    ProviderReasoningDelta,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
    ProviderUsage,
)
from app.services.harness.providers.bedrock_compiler import (
    BedrockCompileError,
    BedrockCompileErrorCode,
    BedrockConverseStreamCompiler,
)
from app.services.harness.providers.bedrock_decode_support import (
    BedrockDecodeError,
    BedrockDecodeErrorCode,
)
from app.services.harness.providers.bedrock_decoder import (
    BedrockConverseStreamDecoder,
)
from tests.harness_provider_capability_fixtures import model, request

FIXTURES = Path(__file__).parent / "fixtures" / "harness" / "bedrock_converse"


def bedrock_model():
    return model().model_copy(
        update={
            "provider": "bedrock",
            "model": "amazon.nova-lite-v1:0",
            "context_features": (),
        }
    )


def context(canonical_request=None) -> ProviderContextPlan:
    selected_request = canonical_request or request()
    return ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(
            selected_request.model_dump_json().encode()
        ).hexdigest(),
        model_revision_sha256=bedrock_model().model_revision_sha256,
        applied_features=(),
        estimated_input_tokens=10,
        reason="Compiled Bedrock test context.",
    )


def test_compiler_maps_system_text_tools_and_limits_to_boto_shape() -> None:
    canonical_request = request()
    system_message = ProviderMessage(
        role=ProviderMessageRole.SYSTEM,
        parts=canonical_request.messages[0].parts,
    )
    schema_json = (
        '{"additionalProperties":false,"properties":{"city":'
        '{"type":"string"}},"required":["city"],"type":"object"}'
    )
    tool = ProviderToolDefinition(
        name="weather_lookup",
        version="1.0.0",
        description="Look up weather.",
        input_schema_json=schema_json,
        input_schema_sha256=hashlib.sha256(schema_json.encode()).hexdigest(),
    )
    canonical_request = canonical_request.model_copy(
        update={
            "messages": (system_message, *canonical_request.messages),
            "tools": (tool,),
        }
    )

    compiled = BedrockConverseStreamCompiler().compile(
        canonical_request,
        bedrock_model(),
        context(canonical_request),
    )
    wire = compiled.to_boto_request()

    assert wire["modelId"] == "amazon.nova-lite-v1:0"
    assert wire["inferenceConfig"] == {"maxTokens": 1_024}
    assert wire["system"] == [{"text": "independent provider capabilities"}]
    assert wire["messages"][0]["role"] == "user"
    assert wire["toolConfig"]["tools"][0]["toolSpec"]["strict"] is True


def test_compiler_rejects_late_system_and_unsupported_tool_name() -> None:
    late_system = ProviderMessage(
        role=ProviderMessageRole.SYSTEM,
        parts=request().messages[0].parts,
    )
    late_request = request().model_copy(
        update={"messages": (*request().messages, late_system)}
    )
    with pytest.raises(BedrockCompileError) as late:
        BedrockConverseStreamCompiler().compile(
            late_request,
            bedrock_model(),
            context(late_request),
        )
    assert late.value.code is BedrockCompileErrorCode.MESSAGE

    schema_json = "{}"
    dotted_tool = ProviderToolDefinition(
        name="weather.lookup",
        version="1.0.0",
        description="Look up weather.",
        input_schema_json=schema_json,
        input_schema_sha256=hashlib.sha256(schema_json.encode()).hexdigest(),
    )
    tool_request = request().model_copy(update={"tools": (dotted_tool,)})
    with pytest.raises(BedrockCompileError) as unsupported:
        BedrockConverseStreamCompiler().compile(
            tool_request,
            bedrock_model(),
            context(tool_request),
        )
    assert unsupported.value.code is BedrockCompileErrorCode.TOOL_NAME


def test_text_reasoning_usage_and_metadata_are_normalized() -> None:
    decoder = BedrockConverseStreamDecoder(
        lambda input_tokens, cached, output_tokens, reasoning: (
            input_tokens + output_tokens
        )
    )

    events = _decode_fixture(decoder, "happy.jsonl")

    assert isinstance(events[0], ProviderReasoningDelta)
    assert isinstance(events[1], ProviderTextDelta)
    assert events[1].text == "Hello from Bedrock."
    assert isinstance(events[2], ProviderUsage)
    assert events[2].usage.cached_input_tokens == 3
    assert events[2].usage.cost_microusd == 16
    assert isinstance(events[3], ProviderCompleted)
    assert events[3].finish_reason is ProviderFinishReason.STOP
    assert decoder.metadata is not None
    assert decoder.metadata.latency_ms == 42
    assert decoder.metadata.cache_write_input_tokens == 2
    assert decoder.metadata.reasoning_signature_sha256 is not None
    assert decoder.metadata.provider_metadata_sha256 is not None


def test_fragmented_tool_input_preserves_opaque_id_and_canonical_json() -> None:
    decoder = BedrockConverseStreamDecoder(lambda *counts: 0)

    events = _decode_fixture(decoder, "tool.jsonl")

    assert isinstance(events[0], ProviderToolCall)
    assert events[0].call_id == "tooluse_A1.09:test"
    assert events[0].tool_name == "weather_lookup"
    assert events[0].arguments_json == '{"city":"Pune"}'
    assert isinstance(events[2], ProviderCompleted)
    assert events[2].finish_reason is ProviderFinishReason.TOOL_CALLS


def test_throttling_malformed_order_and_cancellation_fail_closed() -> None:
    throttled = BedrockConverseStreamDecoder(lambda *counts: 0)
    error_events = _decode_fixture(throttled, "throttled.jsonl")
    assert isinstance(error_events[0], ProviderError)
    assert error_events[0].failure_class is ProviderFailureClass.RATE_LIMIT
    assert "fixture secret" not in error_events[0].reason

    malformed = BedrockConverseStreamDecoder(lambda *counts: 0)
    with pytest.raises(BedrockDecodeError) as captured:
        _decode_fixture(malformed, "malformed.jsonl")
    assert captured.value.code is BedrockDecodeErrorCode.SEQUENCE
    with pytest.raises(BedrockDecodeError) as terminal:
        malformed.decode({"messageStart": {"role": "assistant"}})
    assert terminal.value.code is BedrockDecodeErrorCode.TERMINAL

    cancelled = BedrockConverseStreamDecoder(lambda *counts: 0)
    event = cancelled.cancel()
    assert isinstance(event, ProviderCancelled)
    with pytest.raises(BedrockDecodeError):
        cancelled.decode({"messageStart": {"role": "assistant"}})


def _decode_fixture(
    decoder: BedrockConverseStreamDecoder,
    fixture_name: str,
):
    events = []
    for line in (FIXTURES / fixture_name).read_text().splitlines():
        events.extend(decoder.decode(json.loads(line)))
    return events
