"""Shared deadline, cancellation, and capacity control for provider streams."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum


class ProviderStreamControlErrorCode(StrEnum):
    CANCELLED = "cancelled"
    DEADLINE = "deadline"


class ProviderStreamControlError(RuntimeError):
    def __init__(self, code: ProviderStreamControlErrorCode) -> None:
        super().__init__("provider stream control rejected the operation")
        self.code = code


def remaining_seconds(
    clock: Callable[[], datetime],
    deadline_at: datetime,
) -> float:
    now = clock()
    if (
        now.tzinfo is None
        or now.utcoffset() != timedelta(0)
        or deadline_at.tzinfo is None
        or deadline_at.utcoffset() != timedelta(0)
        or deadline_at <= now
    ):
        raise ProviderStreamControlError(
            ProviderStreamControlErrorCode.DEADLINE
        )
    return (deadline_at - now).total_seconds()


async def acquire_stream_capacity(
    capacity: asyncio.Semaphore,
    cancellation: asyncio.Event,
    timeout_seconds: float,
) -> None:
    if cancellation.is_set():
        raise ProviderStreamControlError(
            ProviderStreamControlErrorCode.CANCELLED
        )
    acquire_task = asyncio.create_task(capacity.acquire())
    cancellation_task = asyncio.create_task(cancellation.wait())
    acquisition_accepted = False
    try:
        done, _ = await asyncio.wait(
            (acquire_task, cancellation_task),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            raise ProviderStreamControlError(
                ProviderStreamControlErrorCode.CANCELLED
            )
        if acquire_task not in done:
            raise ProviderStreamControlError(
                ProviderStreamControlErrorCode.DEADLINE
            )
        acquire_task.result()
        acquisition_accepted = True
    finally:
        await _cancel_future(cancellation_task)
        if not acquisition_accepted:
            await _revoke_acquisition(acquire_task, capacity)


async def _cancel_future[FutureResult](
    future: asyncio.Future[FutureResult],
) -> None:
    if not future.done():
        future.cancel()
    await asyncio.gather(future, return_exceptions=True)


async def _revoke_acquisition(
    task: asyncio.Task[bool],
    capacity: asyncio.Semaphore,
) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    if task.cancelled():
        return
    try:
        acquired = task.result()
    except Exception:
        return
    if acquired:
        capacity.release()
