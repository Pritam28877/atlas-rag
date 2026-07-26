"""Bounded live-event delivery with durable-journal resynchronization."""

from __future__ import annotations

import asyncio
from collections import deque
from enum import StrEnum
from typing import Annotated, Literal, Never

from pydantic import Field

from app.services.harness.protocol import (
    EventRecord,
    EventSubscribeCommand,
    StrictProtocolModel,
    SubscriptionId,
)

MAXIMUM_BUFFERED_EVENTS = 2_048
MAXIMUM_JOURNAL_SEQUENCE = 2**63 - 1


class SubscriptionState(StrEnum):
    OPEN = "open"
    RESYNC_REQUIRED = "resync_required"
    CLOSED = "closed"


class PublishDisposition(StrEnum):
    ENQUEUED = "enqueued"
    FILTERED = "filtered"
    RESYNC_REQUIRED = "resync_required"


class SubscriptionErrorCode(StrEnum):
    CLOSED = "closed"
    INVALID_ACKNOWLEDGEMENT = "invalid_acknowledgement"
    INVALID_BATCH_LIMIT = "invalid_batch_limit"
    INVALID_RESUME_SEQUENCE = "invalid_resume_sequence"
    OUT_OF_ORDER_EVENT = "out_of_order_event"


class SubscriptionError(RuntimeError):
    """Structured failure safe to translate at the transport boundary."""

    def __init__(self, code: SubscriptionErrorCode) -> None:
        super().__init__("subscription operation rejected")
        self.code = code


class SequencedEvent(StrictProtocolModel):
    """One event tied to its durable global journal position."""

    kind: Literal["event"] = "event"
    journal_sequence: int = Field(ge=1, le=MAXIMUM_JOURNAL_SEQUENCE)
    event: EventRecord


class ResyncRequired(StrictProtocolModel):
    """Bounded marker directing a client back to durable journal replay."""

    kind: Literal["resync_required"] = "resync_required"
    subscription_id: SubscriptionId
    resume_after_sequence: int = Field(ge=0, le=MAXIMUM_JOURNAL_SEQUENCE)
    latest_observed_sequence: int = Field(ge=1, le=MAXIMUM_JOURNAL_SEQUENCE)


type SubscriptionDelivery = Annotated[
    SequencedEvent | ResyncRequired,
    Field(discriminator="kind"),
]


class SubscriptionSnapshot(StrictProtocolModel):
    state: SubscriptionState
    acknowledged_sequence: int = Field(ge=0, le=MAXIMUM_JOURNAL_SEQUENCE)
    delivered_sequence: int = Field(ge=0, le=MAXIMUM_JOURNAL_SEQUENCE)
    latest_observed_sequence: int = Field(ge=0, le=MAXIMUM_JOURNAL_SEQUENCE)
    buffered_deliveries: int = Field(ge=0, le=MAXIMUM_BUFFERED_EVENTS)


class _SubscriptionIdentity(StrictProtocolModel):
    subscription_id: SubscriptionId


class EventSubscription:
    """Owns one bounded subscriber channel and its ephemeral delivery cursor."""

    def __init__(
        self,
        subscription_id: SubscriptionId,
        command: EventSubscribeCommand,
        *,
        maximum_buffered_events: int = 256,
    ) -> None:
        if not 1 <= maximum_buffered_events <= MAXIMUM_BUFFERED_EVENTS:
            raise ValueError(
                "maximum_buffered_events must be between 1 and 2048"
            )
        self._subscription_id = _SubscriptionIdentity(
            subscription_id=subscription_id
        ).subscription_id
        self._command = command
        self._capacity = maximum_buffered_events
        self._deliveries: deque[SubscriptionDelivery] = deque()
        self._state = SubscriptionState.OPEN
        self._acknowledged_sequence = command.after_sequence
        self._delivered_sequence = command.after_sequence
        self._latest_observed_sequence = command.after_sequence
        self._lock = asyncio.Lock()

    async def publish(self, delivery: SequencedEvent) -> PublishDisposition:
        async with self._lock:
            self._require_open_or_resync()
            if delivery.journal_sequence <= self._latest_observed_sequence:
                self._reject(SubscriptionErrorCode.OUT_OF_ORDER_EVENT)
            self._latest_observed_sequence = delivery.journal_sequence

            if self._state is SubscriptionState.RESYNC_REQUIRED:
                self._replace_with_resync_marker()
                return PublishDisposition.RESYNC_REQUIRED
            if (
                self._command.event_types
                and delivery.event.event_type not in self._command.event_types
            ):
                return PublishDisposition.FILTERED
            if len(self._deliveries) >= self._capacity:
                self._state = SubscriptionState.RESYNC_REQUIRED
                self._replace_with_resync_marker()
                return PublishDisposition.RESYNC_REQUIRED

            self._deliveries.append(delivery)
            return PublishDisposition.ENQUEUED

    async def take(self, maximum_items: int) -> tuple[SubscriptionDelivery, ...]:
        async with self._lock:
            self._require_open_or_resync()
            if not 1 <= maximum_items <= self._command.max_batch_size:
                self._reject(SubscriptionErrorCode.INVALID_BATCH_LIMIT)

            item_count = min(maximum_items, len(self._deliveries))
            deliveries = tuple(
                self._deliveries.popleft() for _ in range(item_count)
            )
            for delivery in deliveries:
                if isinstance(delivery, SequencedEvent):
                    self._delivered_sequence = delivery.journal_sequence
            return deliveries

    async def acknowledge(self, through_sequence: int) -> int:
        async with self._lock:
            self._require_open_or_resync()
            if (
                through_sequence < self._acknowledged_sequence
                or through_sequence > self._delivered_sequence
            ):
                self._reject(SubscriptionErrorCode.INVALID_ACKNOWLEDGEMENT)
            self._acknowledged_sequence = through_sequence
            return self._acknowledged_sequence

    async def resume(self, after_sequence: int) -> None:
        async with self._lock:
            self._require_open_or_resync()
            if (
                self._state is not SubscriptionState.RESYNC_REQUIRED
                or after_sequence != self._latest_observed_sequence
            ):
                self._reject(SubscriptionErrorCode.INVALID_RESUME_SEQUENCE)
            self._deliveries.clear()
            self._acknowledged_sequence = after_sequence
            self._delivered_sequence = after_sequence
            self._latest_observed_sequence = after_sequence
            self._state = SubscriptionState.OPEN

    async def close(self) -> None:
        async with self._lock:
            self._deliveries.clear()
            self._state = SubscriptionState.CLOSED

    async def snapshot(self) -> SubscriptionSnapshot:
        async with self._lock:
            return SubscriptionSnapshot(
                state=self._state,
                acknowledged_sequence=self._acknowledged_sequence,
                delivered_sequence=self._delivered_sequence,
                latest_observed_sequence=self._latest_observed_sequence,
                buffered_deliveries=len(self._deliveries),
            )

    def _replace_with_resync_marker(self) -> None:
        self._deliveries.clear()
        self._deliveries.append(
            ResyncRequired(
                subscription_id=self._subscription_id,
                resume_after_sequence=self._acknowledged_sequence,
                latest_observed_sequence=self._latest_observed_sequence,
            )
        )

    def _require_open_or_resync(self) -> None:
        if self._state is SubscriptionState.CLOSED:
            self._reject(SubscriptionErrorCode.CLOSED)

    @staticmethod
    def _reject(code: SubscriptionErrorCode) -> Never:
        raise SubscriptionError(code)
