import asyncio
from collections.abc import AsyncIterator
from enum import Enum

import pytest

from app.cli.harness.adapter_smoke_runtime import (
    verify_pre_cancelled_adapter_stream,
)


class CancellationCode(Enum):
    CANCELLED = "cancelled"
    PROVIDER = "provider"


class StreamFailure(Exception):
    def __init__(self, code: CancellationCode) -> None:
        super().__init__(code.value)
        self.code = code


class FailedStream:
    def __init__(self, code: CancellationCode) -> None:
        self._code = code

    def __aiter__(self) -> "FailedStream":
        return self

    async def __anext__(self) -> object:
        raise StreamFailure(self._code)


async def _event_stream() -> AsyncIterator[object]:
    yield object()


def test_pre_cancelled_stream_records_bounded_latency() -> None:
    times = iter((1.0, 1.004))

    latency_ms = asyncio.run(
        verify_pre_cancelled_adapter_stream(
            FailedStream(CancellationCode.CANCELLED),
            expected_error_code=CancellationCode.CANCELLED,
            monotonic_clock=times.__next__,
        )
    )

    assert latency_ms == 4


def test_pre_cancelled_stream_rejects_unexpected_error() -> None:
    with pytest.raises(StreamFailure):
        asyncio.run(
            verify_pre_cancelled_adapter_stream(
                FailedStream(CancellationCode.PROVIDER),
                expected_error_code=CancellationCode.CANCELLED,
                monotonic_clock=lambda: 1.0,
            )
        )


def test_pre_cancelled_stream_rejects_emitted_event() -> None:
    with pytest.raises(
        ValueError,
        match="pre-cancelled adapter smoke emitted an event",
    ):
        asyncio.run(
            verify_pre_cancelled_adapter_stream(
                _event_stream(),
                expected_error_code=CancellationCode.CANCELLED,
                monotonic_clock=lambda: 1.0,
            )
        )
