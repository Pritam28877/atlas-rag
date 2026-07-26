import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.providers import (
    CancellableProviderRetryDelay,
    ProviderRetryDelayError,
)


def deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=1)


def test_retry_delay_completes_without_retaining_tasks() -> None:
    async def scenario() -> None:
        await CancellableProviderRetryDelay().wait(
            1,
            cancellation=asyncio.Event(),
            deadline_at=deadline(),
        )
        current = asyncio.current_task()
        remaining = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        assert remaining == []

    asyncio.run(scenario())


def test_retry_delay_cancellation_joins_sleep_task() -> None:
    async def scenario() -> None:
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            CancellableProviderRetryDelay().wait(
                500,
                cancellation=cancellation,
                deadline_at=deadline(),
            )
        )
        await asyncio.sleep(0)
        cancellation.set()
        with pytest.raises(ProviderRetryDelayError):
            await task
        assert task.done()

    asyncio.run(scenario())
