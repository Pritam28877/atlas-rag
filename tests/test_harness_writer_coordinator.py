import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.sessions import (
    SessionWriterCoordinator,
    WakeDisposition,
    WriterCoordinatorError,
    WriterCoordinatorErrorCode,
)

NOW = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
FIRST_THREAD = "thr_" + "1" * 32
SECOND_THREAD = "thr_" + "2" * 32
THIRD_THREAD = "thr_" + "3" * 32
FIRST_OWNER = "a" * 64
SECOND_OWNER = "b" * 64


def test_expiry_handoff_fences_old_writer_before_apply() -> None:
    async def scenario() -> None:
        coordinator = SessionWriterCoordinator(lease_seconds=2)
        first = await coordinator.acquire(
            FIRST_THREAD,
            FIRST_OWNER,
            acquired_at=NOW,
        )
        with pytest.raises(WriterCoordinatorError) as busy:
            await coordinator.acquire(
                FIRST_THREAD,
                SECOND_OWNER,
                acquired_at=NOW + timedelta(seconds=1),
            )
        second = await coordinator.acquire(
            FIRST_THREAD,
            SECOND_OWNER,
            acquired_at=NOW + timedelta(seconds=2),
        )
        outcomes = await asyncio.gather(
            coordinator.validate_fence(
                first,
                applied_at=NOW + timedelta(seconds=2),
            ),
            coordinator.validate_fence(
                second,
                applied_at=NOW + timedelta(seconds=2),
            ),
            return_exceptions=True,
        )

        assert busy.value.code is WriterCoordinatorErrorCode.BUSY
        assert second.fencing_generation > first.fencing_generation
        assert sum(isinstance(value, WriterCoordinatorError) for value in outcomes) == 1
        assert second.fencing_generation in outcomes

    asyncio.run(scenario())


def test_concurrent_acquisition_has_exactly_one_writer() -> None:
    async def scenario() -> None:
        coordinator = SessionWriterCoordinator()
        outcomes = await asyncio.gather(
            coordinator.acquire(FIRST_THREAD, FIRST_OWNER, acquired_at=NOW),
            coordinator.acquire(FIRST_THREAD, SECOND_OWNER, acquired_at=NOW),
            return_exceptions=True,
        )

        assert sum(
            not isinstance(value, BaseException) for value in outcomes
        ) == 1
        errors = tuple(
            value
            for value in outcomes
            if isinstance(value, WriterCoordinatorError)
        )
        assert len(errors) == 1
        assert errors[0].code is WriterCoordinatorErrorCode.BUSY

    asyncio.run(scenario())


def test_wakes_coalesce_without_an_owned_queue() -> None:
    async def scenario() -> None:
        coordinator = SessionWriterCoordinator()
        lease = await coordinator.acquire(
            FIRST_THREAD,
            FIRST_OWNER,
            acquired_at=NOW,
        )

        assert await coordinator.request_wake(
            FIRST_THREAD,
            requested_at=NOW,
        ) is (
            WakeDisposition.ENQUEUED
        )
        for _ in range(100):
            assert await coordinator.request_wake(
                FIRST_THREAD,
                requested_at=NOW,
            ) is (
                WakeDisposition.COALESCED
            )
        assert await coordinator.take_wake(lease, taken_at=NOW)
        assert not await coordinator.take_wake(lease, taken_at=NOW)

    asyncio.run(scenario())


def test_capacity_is_bounded_and_release_frees_it() -> None:
    async def scenario() -> None:
        coordinator = SessionWriterCoordinator(maximum_active_sessions=2)
        first = await coordinator.acquire(
            FIRST_THREAD,
            FIRST_OWNER,
            acquired_at=NOW,
        )
        await coordinator.acquire(
            SECOND_THREAD,
            SECOND_OWNER,
            acquired_at=NOW,
        )
        with pytest.raises(WriterCoordinatorError) as capacity:
            await coordinator.acquire(
                THIRD_THREAD,
                "c" * 64,
                acquired_at=NOW,
            )
        await coordinator.release(first, released_at=NOW)
        third = await coordinator.acquire(
            THIRD_THREAD,
            "c" * 64,
            acquired_at=NOW,
        )

        assert capacity.value.code is WriterCoordinatorErrorCode.CAPACITY
        assert await coordinator.active_sessions(observed_at=NOW) == 2
        assert third.fencing_generation > first.fencing_generation

    asyncio.run(scenario())


def test_renewal_and_reacquire_are_idempotent_for_same_owner() -> None:
    async def scenario() -> None:
        coordinator = SessionWriterCoordinator(lease_seconds=2)
        first = await coordinator.acquire(
            FIRST_THREAD,
            FIRST_OWNER,
            acquired_at=NOW,
        )
        replay = await coordinator.acquire(
            FIRST_THREAD,
            FIRST_OWNER,
            acquired_at=NOW + timedelta(seconds=1),
        )
        renewed = await coordinator.renew(
            replay,
            renewed_at=NOW + timedelta(seconds=1),
        )

        assert replay == first
        assert renewed.fencing_generation == first.fencing_generation
        assert renewed.expires_at == NOW + timedelta(seconds=3)
        assert await coordinator.validate_fence(
            renewed,
            applied_at=NOW + timedelta(seconds=2),
        ) == first.fencing_generation

    asyncio.run(scenario())


def test_expired_idle_session_is_pruned_without_reusing_fence() -> None:
    async def scenario() -> None:
        coordinator = SessionWriterCoordinator(
            maximum_active_sessions=1,
            lease_seconds=1,
        )
        first = await coordinator.acquire(
            FIRST_THREAD,
            FIRST_OWNER,
            acquired_at=NOW,
        )
        second = await coordinator.acquire(
            SECOND_THREAD,
            SECOND_OWNER,
            acquired_at=NOW + timedelta(seconds=1),
        )

        assert await coordinator.active_sessions(
            observed_at=NOW + timedelta(seconds=1),
        ) == 1
        assert second.fencing_generation > first.fencing_generation
        with pytest.raises(WriterCoordinatorError) as fenced:
            await coordinator.validate_fence(
                first,
                applied_at=NOW + timedelta(seconds=1),
            )
        assert fenced.value.code is WriterCoordinatorErrorCode.FENCED

    asyncio.run(scenario())
