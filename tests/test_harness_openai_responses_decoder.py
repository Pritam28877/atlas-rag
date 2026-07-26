import json
from pathlib import Path

import pytest

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderFailureClass,
    ProviderFinishReason,
    ProviderReasoningDelta,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers import (
    MAXIMUM_OPENAI_WIRE_EVENT_BYTES,
    OpenAIDecodeError,
    OpenAIDecodeErrorCode,
    OpenAIResponsesDecoder,
)

FIXTURES = (
    Path(__file__).parent / "fixtures" / "harness" / "openai_responses"
)


def records(name: str) -> tuple[bytes, ...]:
    return tuple((FIXTURES / name).read_bytes().splitlines())


def decode_fixture(
    name: str,
) -> tuple[tuple[ProviderStreamEvent, ...], list[tuple[int, ...]]]:
    cost_inputs: list[tuple[int, ...]] = []

    def cost(
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> int:
        values = (
            input_tokens,
            cached_tokens,
            output_tokens,
            reasoning_tokens,
        )
        cost_inputs.append(values)
        return input_tokens + output_tokens + reasoning_tokens

    decoder = OpenAIResponsesDecoder(cost)
    events: list[ProviderStreamEvent] = []
    for record in records(name):
        events.extend(decoder.decode(record))
    return tuple(events), cost_inputs


def test_decodes_reasoning_text_usage_and_completion() -> None:
    events, cost_inputs = decode_fixture("happy.jsonl")

    assert isinstance(events[0], ProviderReasoningDelta)
    assert events[0].text == "Checked bounded evidence."
    assert isinstance(events[1], ProviderTextDelta)
    assert events[1].text == "Hello from OpenAI."
    assert isinstance(events[2], ProviderUsage)
    assert events[2].usage.input_tokens == 11
    assert events[2].usage.cached_input_tokens == 3
    assert events[2].usage.output_tokens == 5
    assert events[2].usage.reasoning_tokens == 2
    assert events[2].usage.cost_microusd == 18
    assert isinstance(events[3], ProviderCompleted)
    assert events[3].finish_reason is ProviderFinishReason.STOP
    assert tuple(event.sequence for event in events) == (1, 2, 3, 4)
    assert cost_inputs == [(11, 3, 5, 2)]


def test_parallel_tool_calls_preserve_ids_and_canonicalize_arguments() -> None:
    events, cost_inputs = decode_fixture("tools.jsonl")
    first = events[0]
    second = events[1]

    assert isinstance(first, ProviderToolCall)
    assert first.call_id == "call_weather-A1"
    assert first.tool_name == "weather.lookup"
    assert first.arguments_json == '{"city":"Pune","units":"c"}'
    assert isinstance(second, ProviderToolCall)
    assert second.call_id == "call_clock_B2"
    assert second.tool_name == "clock.read"
    assert isinstance(events[2], ProviderUsage)
    assert isinstance(events[3], ProviderCompleted)
    assert events[3].finish_reason is ProviderFinishReason.TOOL_CALLS
    assert cost_inputs == [(20, 0, 9, 0)]


@pytest.mark.parametrize(
    ("fixture", "failure_class", "retry_allowed"),
    (
        ("rate_limit.jsonl", ProviderFailureClass.RATE_LIMIT, True),
        (
            "context_overflow.jsonl",
            ProviderFailureClass.CONTEXT_LENGTH,
            False,
        ),
    ),
)
def test_provider_errors_are_classified_without_raw_message(
    fixture: str,
    failure_class: ProviderFailureClass,
    retry_allowed: bool,
) -> None:
    events, cost_inputs = decode_fixture(fixture)
    error = events[0]

    assert isinstance(error, ProviderError)
    assert error.failure_class is failure_class
    assert error.retry_allowed is retry_allowed
    assert "fixture message" not in error.reason
    assert cost_inputs == []


def test_output_limit_emits_usage_then_length_completion() -> None:
    events, cost_inputs = decode_fixture("incomplete.jsonl")

    assert isinstance(events[0], ProviderUsage)
    assert isinstance(events[1], ProviderCompleted)
    assert events[1].finish_reason is ProviderFinishReason.LENGTH
    assert cost_inputs == [(7, 1, 4, 1)]


def test_explicit_cancellation_is_terminal() -> None:
    decoder = OpenAIResponsesDecoder(lambda *counts: 0)

    cancelled = decoder.cancel()

    assert isinstance(cancelled, ProviderCancelled)
    assert cancelled.sequence == 1
    with pytest.raises(OpenAIDecodeError) as captured:
        decoder.decode(records("happy.jsonl")[0])
    assert captured.value.code is OpenAIDecodeErrorCode.TERMINAL


def test_malformed_sequence_size_and_unsupported_events_fail_closed() -> None:
    decoder = OpenAIResponsesDecoder(lambda *counts: 0)
    with pytest.raises(OpenAIDecodeError) as malformed:
        decoder.decode(records("malformed.jsonl")[0])
    assert malformed.value.code is OpenAIDecodeErrorCode.MALFORMED
    assert str(malformed.value) == "OpenAI Responses stream decoding failed"

    oversized = OpenAIResponsesDecoder(lambda *counts: 0)
    with pytest.raises(OpenAIDecodeError) as size_error:
        oversized.decode(b"x" * (MAXIMUM_OPENAI_WIRE_EVENT_BYTES + 1))
    assert size_error.value.code is OpenAIDecodeErrorCode.EVENT_SIZE

    unsupported = OpenAIResponsesDecoder(lambda *counts: 0)
    built_in = json.dumps(
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "item": {"type": "file_search_call"},
        },
        separators=(",", ":"),
    ).encode()
    with pytest.raises(OpenAIDecodeError) as unsupported_error:
        unsupported.decode(built_in)
    assert unsupported_error.value.code is OpenAIDecodeErrorCode.UNSUPPORTED


def test_provider_sequence_gap_and_event_count_are_bounded() -> None:
    sequence_decoder = OpenAIResponsesDecoder(lambda *counts: 0)
    sequence_decoder.decode(
        b'{"sequence_number":1,"type":"response.created"}'
    )
    with pytest.raises(OpenAIDecodeError) as gap:
        sequence_decoder.decode(
            b'{"sequence_number":3,"type":"response.in_progress"}'
        )
    assert gap.value.code is OpenAIDecodeErrorCode.SEQUENCE

    count_decoder = OpenAIResponsesDecoder(lambda *counts: 0)
    for sequence in range(1, 4_097):
        record = json.dumps(
            {
                "sequence_number": sequence,
                "type": "response.in_progress",
            },
            separators=(",", ":"),
        ).encode()
        assert count_decoder.decode(record) == ()
    overflow = (
        b'{"sequence_number":4097,"type":"response.in_progress"}'
    )
    with pytest.raises(OpenAIDecodeError) as event_limit:
        count_decoder.decode(overflow)
    assert event_limit.value.code is OpenAIDecodeErrorCode.EVENT_LIMIT
