"""Storage-neutral contracts for the append-only Atlas event journal."""

from __future__ import annotations

from enum import StrEnum
from typing import Never, Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    AggregateId,
    EventId,
    EventRecord,
    IdempotencyKey,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)

MAXIMUM_APPEND_EVENTS = 256
MAXIMUM_APPEND_BYTES = 4 * 1024 * 1024
MAXIMUM_SEQUENCE = 2**63 - 1


class JournalDurability(StrEnum):
    SYNCHRONOUS = "synchronous"
    BUFFERED = "buffered"


class AppendStatus(StrEnum):
    APPENDED = "appended"
    IDEMPOTENT_REPLAY = "idempotent_replay"


class JournalConflictCode(StrEnum):
    EXPECTED_SEQUENCE = "expected_sequence"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"


class JournalConflictError(RuntimeError):
    """Stable optimistic-concurrency failure without storage detail."""

    def __init__(
        self,
        code: JournalConflictCode,
        *,
        current_sequence: int,
    ) -> None:
        super().__init__("journal append conflict")
        self.code = code
        self.current_sequence = current_sequence


class AppendRequest(StrictProtocolModel):
    aggregate_id: AggregateId
    expected_sequence: int = Field(ge=0, le=MAXIMUM_SEQUENCE)
    idempotency_key: IdempotencyKey
    request_sha256: Sha256
    durability: JournalDurability = JournalDurability.SYNCHRONOUS
    events: tuple[EventRecord, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_APPEND_EVENTS,
    )

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        event_ids: set[EventId] = set()
        previous_time = None
        serialized_bytes = 0
        for event_offset, event in enumerate(self.events, start=1):
            if event.aggregate_id != self.aggregate_id:
                raise ValueError("append event belongs to another aggregate")
            expected_event_sequence = self.expected_sequence + event_offset
            if expected_event_sequence > MAXIMUM_SEQUENCE:
                raise ValueError("append sequence exceeds signed 64-bit range")
            if event.aggregate_sequence != expected_event_sequence:
                raise ValueError("append event sequences must be contiguous")
            if event.event_id in event_ids:
                raise ValueError("append event IDs must be unique")
            event_ids.add(event.event_id)
            if previous_time is not None and event.occurred_at < previous_time:
                raise ValueError("append event timestamps must be nondecreasing")
            previous_time = event.occurred_at
            serialized_bytes += len(event.model_dump_json().encode())
            if serialized_bytes > MAXIMUM_APPEND_BYTES:
                raise ValueError("append batch exceeds 4 MiB serialized limit")
        return self


class AppendResult(StrictProtocolModel):
    aggregate_id: AggregateId
    status: AppendStatus
    durability: JournalDurability
    first_sequence: int = Field(ge=1, le=MAXIMUM_SEQUENCE)
    last_sequence: int = Field(ge=1, le=MAXIMUM_SEQUENCE)
    event_ids: tuple[EventId, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_APPEND_EVENTS,
    )
    request_sha256: Sha256
    receipt_sha256: Sha256
    committed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_sequence_span(self) -> Self:
        sequence_count = self.last_sequence - self.first_sequence + 1
        if sequence_count != len(self.event_ids):
            raise ValueError("append result sequence span must match event IDs")
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("append result event IDs must be unique")
        return self


class JournalPage(StrictProtocolModel):
    aggregate_id: AggregateId
    after_sequence: int = Field(ge=0, le=MAXIMUM_SEQUENCE)
    events: tuple[EventRecord, ...] = Field(max_length=MAXIMUM_APPEND_EVENTS)
    has_more: bool

    @model_validator(mode="after")
    def validate_page_order(self) -> Self:
        expected_sequence = self.after_sequence + 1
        for event in self.events:
            if event.aggregate_id != self.aggregate_id:
                raise ValueError("journal page event belongs to another aggregate")
            if event.aggregate_sequence != expected_sequence:
                raise ValueError("journal page sequences must be contiguous")
            expected_sequence += 1
        if self.has_more and not self.events:
            raise ValueError("journal page with more data cannot be empty")
        return self


class JournalReadRequest(StrictProtocolModel):
    aggregate_id: AggregateId
    after_sequence: int = Field(ge=0, le=MAXIMUM_SEQUENCE)
    limit: int = Field(default=256, ge=1, le=MAXIMUM_APPEND_EVENTS)


class EventJournal(Protocol):
    async def append(self, request: AppendRequest) -> AppendResult: ...

    async def read_aggregate(
        self,
        request: JournalReadRequest,
    ) -> JournalPage: ...


def raise_expected_sequence_conflict(current_sequence: int) -> Never:
    raise JournalConflictError(
        JournalConflictCode.EXPECTED_SEQUENCE,
        current_sequence=current_sequence,
    )


def raise_idempotency_conflict(current_sequence: int) -> Never:
    raise JournalConflictError(
        JournalConflictCode.IDEMPOTENCY_MISMATCH,
        current_sequence=current_sequence,
    )
