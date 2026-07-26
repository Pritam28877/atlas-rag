"""Bounded deterministic replay of recorded provider stream bytes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from enum import StrEnum

from pydantic import TypeAdapter, ValidationError

from app.services.harness.protocol import ProviderStreamEvent

MAXIMUM_RECORDED_EVENTS = 4_096
MAXIMUM_RECORDED_EVENT_BYTES = 64 * 1024
MAXIMUM_RECORDED_STREAM_BYTES = 16 * 1024 * 1024


class RecordedProviderErrorCode(StrEnum):
    DECODE = "decode"
    EVENT_LIMIT = "event_limit"
    EVENT_SIZE = "event_size"
    STREAM_SIZE = "stream_size"
    SEQUENCE = "sequence"
    CANCELLED = "cancelled"


class RecordedProviderError(RuntimeError):
    def __init__(self, code: RecordedProviderErrorCode) -> None:
        super().__init__("recorded provider stream failed")
        self.code = code


class RecordedProviderStream:
    def __init__(self, records: Sequence[bytes]) -> None:
        if len(records) > MAXIMUM_RECORDED_EVENTS:
            raise RecordedProviderError(RecordedProviderErrorCode.EVENT_LIMIT)
        immutable_records: list[bytes] = []
        total_bytes = 0
        for record in records:
            if not isinstance(record, bytes):
                raise TypeError("recorded provider events must be bytes")
            if not 1 <= len(record) <= MAXIMUM_RECORDED_EVENT_BYTES:
                raise RecordedProviderError(RecordedProviderErrorCode.EVENT_SIZE)
            total_bytes += len(record)
            if total_bytes > MAXIMUM_RECORDED_STREAM_BYTES:
                raise RecordedProviderError(RecordedProviderErrorCode.STREAM_SIZE)
            immutable_records.append(record)
        self._records = tuple(immutable_records)
        self._adapter: TypeAdapter[ProviderStreamEvent] = TypeAdapter(
            ProviderStreamEvent
        )

    async def events(
        self,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        expected_sequence = 1
        for record in self._records:
            if cancellation is not None and cancellation.is_set():
                raise RecordedProviderError(
                    RecordedProviderErrorCode.CANCELLED
                )
            try:
                event = self._adapter.validate_json(record)
            except ValidationError as error:
                raise RecordedProviderError(
                    RecordedProviderErrorCode.DECODE
                ) from error
            if event.sequence != expected_sequence:
                raise RecordedProviderError(
                    RecordedProviderErrorCode.SEQUENCE
                )
            expected_sequence += 1
            yield event
