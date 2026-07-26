import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    EventSubscribeCommand,
    InlinePayload,
    TraceLink,
)
from app.services.harness.runtime import (
    EventSubscription,
    PublishDisposition,
    ResyncRequired,
    SequencedEvent,
    SubscriptionError,
    SubscriptionErrorCode,
    SubscriptionState,
)

NOW = datetime(2026, 7, 26, 15, 0, tzinfo=UTC)


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def event(sequence: int, event_type: str = "Turn.Accepted") -> SequencedEvent:
    text = f"event-{sequence}"
    encoded = text.encode()
    return SequencedEvent(
        journal_sequence=sequence,
        event=EventRecord(
            event_id=identifier("evt", sequence),
            event_type=event_type,
            schema_version="1.2",
            aggregate_id=identifier("trn"),
            aggregate_sequence=sequence,
            actor_kind=EventActorKind.SYSTEM,
            actor_principal_id=identifier("prn"),
            occurred_at=NOW,
            trace=TraceLink(
                request_id=identifier("req"),
                correlation_id=identifier("evt", 1),
            ),
            payload=InlinePayload(
                text=text,
                size_bytes=len(encoded),
                content_sha256=hashlib.sha256(encoded).hexdigest(),
            ),
        ),
    )


def subscription(
    *,
    after_sequence: int = 0,
    capacity: int = 2,
    event_types: tuple[str, ...] = (),
    max_batch_size: int = 256,
) -> EventSubscription:
    return EventSubscription(
        identifier("sub"),
        EventSubscribeCommand(
            after_sequence=after_sequence,
            max_batch_size=max_batch_size,
            event_types=event_types,
        ),
        maximum_buffered_events=capacity,
    )


def test_slow_subscriber_gets_one_bounded_resync_marker() -> None:
    async def scenario() -> None:
        stream = subscription(capacity=2)
        assert await stream.publish(event(1)) is PublishDisposition.ENQUEUED
        assert await stream.publish(event(2)) is PublishDisposition.ENQUEUED
        assert (
            await stream.publish(event(3))
            is PublishDisposition.RESYNC_REQUIRED
        )

        for sequence in range(4, 10_001):
            assert (
                await stream.publish(event(sequence))
                is PublishDisposition.RESYNC_REQUIRED
            )

        snapshot = await stream.snapshot()
        assert snapshot.state is SubscriptionState.RESYNC_REQUIRED
        assert snapshot.buffered_deliveries == 1
        assert snapshot.latest_observed_sequence == 10_000

        deliveries = await stream.take(1)
        assert deliveries == (
            ResyncRequired(
                subscription_id=identifier("sub"),
                resume_after_sequence=0,
                latest_observed_sequence=10_000,
            ),
        )

    asyncio.run(scenario())


def test_delivery_acknowledgement_and_filtering_are_monotonic() -> None:
    async def scenario() -> None:
        stream = subscription(
            capacity=3,
            event_types=("Turn.Completed",),
            max_batch_size=2,
        )
        assert await stream.publish(event(1)) is PublishDisposition.FILTERED
        assert (
            await stream.publish(event(2, "Turn.Completed"))
            is PublishDisposition.ENQUEUED
        )
        assert (
            await stream.publish(event(3, "Turn.Completed"))
            is PublishDisposition.ENQUEUED
        )

        deliveries = await stream.take(2)
        assert tuple(
            delivery.journal_sequence
            for delivery in deliveries
            if isinstance(delivery, SequencedEvent)
        ) == (2, 3)
        assert await stream.acknowledge(3) == 3
        assert await stream.acknowledge(3) == 3

        with pytest.raises(SubscriptionError) as stale:
            await stream.acknowledge(2)
        assert stale.value.code is SubscriptionErrorCode.INVALID_ACKNOWLEDGEMENT

    asyncio.run(scenario())


def test_acknowledgement_cannot_skip_undelivered_events() -> None:
    async def scenario() -> None:
        stream = subscription()
        await stream.publish(event(1))
        with pytest.raises(SubscriptionError) as invalid:
            await stream.acknowledge(1)
        assert (
            invalid.value.code
            is SubscriptionErrorCode.INVALID_ACKNOWLEDGEMENT
        )

    asyncio.run(scenario())


def test_resync_requires_complete_durable_replay_before_live_resume() -> None:
    async def scenario() -> None:
        stream = subscription(capacity=1)
        await stream.publish(event(1))
        await stream.publish(event(2))
        await stream.publish(event(3))

        for invalid_sequence in (2, 4):
            with pytest.raises(SubscriptionError) as invalid:
                await stream.resume(invalid_sequence)
            assert (
                invalid.value.code
                is SubscriptionErrorCode.INVALID_RESUME_SEQUENCE
            )

        await stream.resume(3)
        assert await stream.publish(event(4)) is PublishDisposition.ENQUEUED
        snapshot = await stream.snapshot()
        assert snapshot.state is SubscriptionState.OPEN
        assert snapshot.acknowledged_sequence == 3
        assert snapshot.buffered_deliveries == 1

    asyncio.run(scenario())


def test_out_of_order_events_and_invalid_batch_limits_fail_closed() -> None:
    async def scenario() -> None:
        stream = subscription(max_batch_size=2)
        await stream.publish(event(1))
        with pytest.raises(SubscriptionError) as duplicate:
            await stream.publish(event(1))
        assert duplicate.value.code is SubscriptionErrorCode.OUT_OF_ORDER_EVENT

        with pytest.raises(SubscriptionError) as oversized:
            await stream.take(3)
        assert oversized.value.code is SubscriptionErrorCode.INVALID_BATCH_LIMIT

    asyncio.run(scenario())


def test_close_releases_buffer_and_rejects_future_operations() -> None:
    async def scenario() -> None:
        stream = subscription()
        await stream.publish(event(1))
        await stream.close()
        snapshot = await stream.snapshot()
        assert snapshot.state is SubscriptionState.CLOSED
        assert snapshot.buffered_deliveries == 0

        with pytest.raises(SubscriptionError) as closed:
            await stream.publish(event(2))
        assert closed.value.code is SubscriptionErrorCode.CLOSED

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", (0, 2_049))
def test_buffer_capacity_has_a_hard_upper_bound(capacity: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 2048"):
        subscription(capacity=capacity)


def test_subscription_identifier_is_validated_at_runtime_boundary() -> None:
    with pytest.raises(ValueError, match="subscription_id"):
        EventSubscription(
            "not-a-subscription-id",  # type: ignore[arg-type]
            EventSubscribeCommand(after_sequence=0),
        )
