import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from app.services.harness.providers import (
    RecordedProviderError,
    RecordedProviderErrorCode,
    RecordedProviderStream,
)

FIXTURES = (
    Path(__file__).parent / "fixtures" / "harness" / "provider_stream"
)


def records(name: str) -> tuple[bytes, ...]:
    return tuple((FIXTURES / name).read_bytes().splitlines())


async def replay(stream: RecordedProviderStream) -> tuple[bytes, ...]:
    output: list[bytes] = []
    async for event in stream.events():
        output.append(event.model_dump_json().encode())
    return tuple(output)


@pytest.mark.parametrize(
    "fixture_name",
    ("happy.jsonl", "tool.jsonl", "error.jsonl", "cancelled.jsonl"),
)
def test_recorded_fixtures_replay_byte_identically(
    fixture_name: str,
) -> None:
    async def scenario() -> None:
        fixture_records = records(fixture_name)
        first = await replay(RecordedProviderStream(fixture_records))
        second = await replay(RecordedProviderStream(fixture_records))

        assert first == second
        assert len(first) == len(fixture_records)

    asyncio.run(scenario())


def test_empty_and_malformed_streams_are_deterministic() -> None:
    async def scenario() -> None:
        assert await replay(RecordedProviderStream(())) == ()
        with pytest.raises(RecordedProviderError) as captured:
            await replay(RecordedProviderStream(records("malformed.jsonl")))
        assert captured.value.code is RecordedProviderErrorCode.DECODE
        assert str(captured.value) == "recorded provider stream failed"

    asyncio.run(scenario())


def test_caller_cancellation_stops_before_decoding() -> None:
    async def scenario() -> None:
        cancellation = asyncio.Event()
        cancellation.set()
        stream = RecordedProviderStream(records("happy.jsonl"))
        with pytest.raises(RecordedProviderError) as captured:
            async for _event in stream.events(cancellation=cancellation):
                raise AssertionError("cancelled stream emitted an event")
        assert captured.value.code is RecordedProviderErrorCode.CANCELLED

    asyncio.run(scenario())


def test_record_count_size_and_sequence_are_bounded() -> None:
    with pytest.raises(RecordedProviderError) as event_limit:
        RecordedProviderStream((b"{}",) * 4_097)
    with pytest.raises(RecordedProviderError) as event_size:
        RecordedProviderStream((b"x" * (64 * 1024 + 1),))
    with pytest.raises(TypeError, match="must be bytes"):
        invalid_records = cast(Sequence[bytes], ([b"{}"],))
        RecordedProviderStream(invalid_records)

    assert event_limit.value.code is RecordedProviderErrorCode.EVENT_LIMIT
    assert event_size.value.code is RecordedProviderErrorCode.EVENT_SIZE

    async def scenario() -> None:
        invalid_sequence = (
            b'{"kind":"text_delta","sequence":2,"text":"late"}',
        )
        with pytest.raises(RecordedProviderError) as captured:
            await replay(RecordedProviderStream(invalid_sequence))
        assert captured.value.code is RecordedProviderErrorCode.SEQUENCE

    asyncio.run(scenario())
