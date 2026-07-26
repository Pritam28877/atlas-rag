"""Cancellation-safe provider retry delay."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta


class ProviderRetryDelayError(RuntimeError):
    """A retry delay was cancelled or exceeded its original deadline."""


class CancellableProviderRetryDelay:
    async def wait(
        self,
        delay_ms: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> None:
        if not 0 <= delay_ms <= 3_600_000:
            raise ValueError("provider retry delay is invalid")
        remaining_seconds = _remaining_seconds(deadline_at)
        delay_seconds = delay_ms / 1_000
        if cancellation.is_set() or delay_seconds >= remaining_seconds:
            raise ProviderRetryDelayError("provider retry delay rejected")
        sleep_task = asyncio.create_task(asyncio.sleep(delay_seconds))
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (sleep_task, cancellation_task),
                timeout=remaining_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done or sleep_task not in done:
                raise ProviderRetryDelayError("provider retry delay rejected")
        finally:
            await _cancel_task(sleep_task)
            await _cancel_task(cancellation_task)


def _remaining_seconds(deadline_at: datetime) -> float:
    now = datetime.now(tz=deadline_at.tzinfo)
    if (
        deadline_at.tzinfo is None
        or deadline_at.utcoffset() != timedelta(0)
    ):
        raise ValueError("provider retry deadline must use UTC")
    remaining = (deadline_at - now).total_seconds()
    if remaining <= 0:
        raise ProviderRetryDelayError("provider retry delay rejected")
    return remaining


async def _cancel_task(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
