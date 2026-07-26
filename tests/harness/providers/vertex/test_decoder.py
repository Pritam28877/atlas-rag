from pathlib import Path

import pytest

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderFailureClass,
    ProviderFinishReason,
    ProviderReasoningDelta,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers.vertex_decode_support import (
    VertexDecodeError,
    VertexDecodeErrorCode,
)
from app.services.harness.providers.vertex_decoder import (
    VertexGenerateContentDecoder,
)

FIXTURES = (
    Path(__file__).parents[3]
    / "fixtures"
    / "harness"
    / "vertex_generate_content"
)


def test_text_reasoning_usage_and_hashed_metadata_are_normalized() -> None:
    decoder = VertexGenerateContentDecoder(
        lambda input_tokens, cached, output_tokens, reasoning: (
            input_tokens + output_tokens + reasoning
        )
    )

    events = _decode_fixture(decoder, "happy.jsonl")

    assert isinstance(events[0], ProviderReasoningDelta)
    assert events[0].text == "I should answer briefly."
    assert isinstance(events[1], ProviderTextDelta)
    assert events[1].text == "Hello from Vertex."
    assert isinstance(events[2], ProviderUsage)
    assert events[2].usage.cached_input_tokens == 3
    assert events[2].usage.reasoning_tokens == 2
    assert events[2].usage.cost_microusd == 17
    assert isinstance(events[3], ProviderCompleted)
    assert events[3].finish_reason is ProviderFinishReason.STOP
    assert decoder.metadata is not None
    assert decoder.metadata.response_id_sha256 is not None
    assert decoder.metadata.model_version_sha256 is not None
    assert decoder.metadata.thought_signature_sha256 is not None
    serialized = decoder.metadata.model_dump_json()
    assert "vertex-response-secret" not in serialized
    assert "opaque-thought-secret" not in serialized


def test_function_call_gets_deterministic_internal_id() -> None:
    first = VertexGenerateContentDecoder(lambda *counts: 0)
    second = VertexGenerateContentDecoder(lambda *counts: 0)

    first_events = _decode_fixture(first, "tool.jsonl")
    second_events = _decode_fixture(second, "tool.jsonl")

    assert isinstance(first_events[0], ProviderToolCall)
    assert isinstance(second_events[0], ProviderToolCall)
    assert first_events[0].call_id == second_events[0].call_id
    assert first_events[0].call_id.startswith("vertex_")
    assert first_events[0].tool_name == "weather_lookup"
    assert first_events[0].arguments_json == '{"city":"Pune"}'
    assert isinstance(first_events[2], ProviderCompleted)
    assert first_events[2].finish_reason is ProviderFinishReason.TOOL_CALLS


def test_safety_and_api_errors_are_classified_without_provider_detail() -> None:
    policy_decoder = VertexGenerateContentDecoder(lambda *counts: 0)
    policy_events = _decode_fixture(policy_decoder, "policy.jsonl")

    assert isinstance(policy_events[1], ProviderError)
    assert policy_events[1].failure_class is ProviderFailureClass.POLICY
    assert "fixture secret" not in policy_events[1].reason
    assert policy_decoder.metadata is not None
    assert policy_decoder.metadata.safety_metadata_sha256 is not None

    rate_decoder = VertexGenerateContentDecoder(lambda *counts: 0)
    error = rate_decoder.decode(
        b'{"error":{"code":429,"message":"secret detail",'
        b'"status":"RESOURCE_EXHAUSTED"}}'
    )[0]
    assert isinstance(error, ProviderError)
    assert error.failure_class is ProviderFailureClass.RATE_LIMIT
    assert error.retry_allowed
    assert "secret detail" not in error.reason


def test_prompt_block_malformed_candidate_and_cancellation_fail_closed() -> None:
    blocked = VertexGenerateContentDecoder(lambda *counts: 0)
    event = blocked.decode(
        b'{"promptFeedback":{"blockReason":"JAILBREAK",'
        b'"blockReasonMessage":"secret detail","safetyRatings":[]}}'
    )[0]
    assert isinstance(event, ProviderError)
    assert event.failure_class is ProviderFailureClass.POLICY
    assert "secret detail" not in event.reason

    malformed = VertexGenerateContentDecoder(lambda *counts: 0)
    with pytest.raises(VertexDecodeError) as captured:
        _decode_fixture(malformed, "malformed.jsonl")
    assert captured.value.code is VertexDecodeErrorCode.SEQUENCE
    with pytest.raises(VertexDecodeError) as terminal:
        malformed.decode(b'{"candidates":[]}')
    assert terminal.value.code is VertexDecodeErrorCode.TERMINAL

    cancelled = VertexGenerateContentDecoder(lambda *counts: 0)
    cancelled_event = cancelled.cancel()
    assert isinstance(cancelled_event, ProviderCancelled)
    with pytest.raises(VertexDecodeError):
        cancelled.decode(b'{"candidates":[]}')


def test_usage_total_must_match_official_component_sum() -> None:
    decoder = VertexGenerateContentDecoder(lambda *counts: 0)
    record = (
        b'{"candidates":[{"finishReason":"STOP","index":0}],'
        b'"usageMetadata":{"candidatesTokenCount":1,'
        b'"promptTokenCount":2,"thoughtsTokenCount":3,'
        b'"toolUsePromptTokenCount":4,"totalTokenCount":999}}'
    )

    with pytest.raises(VertexDecodeError) as captured:
        decoder.decode(record)

    assert captured.value.code is VertexDecodeErrorCode.MALFORMED


def _decode_fixture(
    decoder: VertexGenerateContentDecoder,
    fixture_name: str,
):
    events = []
    for record in (FIXTURES / fixture_name).read_bytes().splitlines():
        events.extend(decoder.decode(record))
    return events
