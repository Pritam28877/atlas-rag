import asyncio

import pytest

from app.services.harness.providers.provider_stream_control import (
    ProviderStreamControlError,
    ProviderStreamControlErrorCode,
    acquire_stream_capacity,
)


def test_simultaneous_cancellation_returns_acquired_capacity() -> None:
    async def scenario() -> None:
        capacity = asyncio.Semaphore(0)
        cancellation = asyncio.Event()
        operation = asyncio.create_task(
            acquire_stream_capacity(capacity, cancellation, 1)
        )
        await asyncio.sleep(0)
        capacity.release()
        cancellation.set()
        with pytest.raises(ProviderStreamControlError) as captured:
            await operation
        assert (
            captured.value.code
            is ProviderStreamControlErrorCode.CANCELLED
        )
        await asyncio.wait_for(capacity.acquire(), timeout=0.1)

    asyncio.run(scenario())


def test_caller_cancellation_removes_pending_capacity_waiter() -> None:
    async def scenario() -> None:
        capacity = asyncio.Semaphore(0)
        operation = asyncio.create_task(
            acquire_stream_capacity(
                capacity,
                asyncio.Event(),
                1,
            )
        )
        await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        capacity.release()
        await asyncio.wait_for(capacity.acquire(), timeout=0.1)

    asyncio.run(scenario())
