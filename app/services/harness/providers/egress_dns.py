"""Bounded cancellation-aware system DNS resolution for provider egress."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum

from app.services.harness.providers.egress_policy import (
    MAXIMUM_EGRESS_ADDRESSES,
)

AddressLookup = Callable[[str, int], Awaitable[tuple[str, ...]]]


class ProviderDnsErrorCode(StrEnum):
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    EMPTY = "empty"
    LIMIT = "limit"
    LOOKUP = "lookup"


class ProviderDnsError(RuntimeError):
    def __init__(self, code: ProviderDnsErrorCode) -> None:
        super().__init__("provider DNS resolution failed")
        self.code = code


class SystemProviderAddressResolver:
    """Resolves without caching so every connection gets fresh policy checks."""

    def __init__(
        self,
        *,
        lookup: AddressLookup | None = None,
        maximum_concurrent_lookups: int = 16,
    ) -> None:
        if not 1 <= maximum_concurrent_lookups <= 64:
            raise ValueError("provider DNS concurrency limit is invalid")
        self._lookup = lookup or _system_lookup
        self._semaphore = asyncio.Semaphore(maximum_concurrent_lookups)

    async def resolve(
        self,
        hostname: str,
        port: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> tuple[str, ...]:
        remaining_seconds = _remaining_seconds(deadline_at)
        if cancellation.is_set():
            raise ProviderDnsError(ProviderDnsErrorCode.CANCELLED)
        try:
            async with asyncio.timeout(remaining_seconds):
                async with self._semaphore:
                    return await self._resolve_bounded(
                        hostname,
                        port,
                        cancellation,
                    )
        except TimeoutError:
            raise ProviderDnsError(
                ProviderDnsErrorCode.DEADLINE
            ) from None

    async def _resolve_bounded(
        self,
        hostname: str,
        port: int,
        cancellation: asyncio.Event,
    ) -> tuple[str, ...]:
        lookup_task: asyncio.Future[tuple[str, ...]] = (
            asyncio.ensure_future(self._lookup(hostname, port))
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (lookup_task, cancellation_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                await _cancel_task(lookup_task)
                raise ProviderDnsError(
                    ProviderDnsErrorCode.CANCELLED
                )
            try:
                addresses = lookup_task.result()
            except Exception:
                raise ProviderDnsError(
                    ProviderDnsErrorCode.LOOKUP
                ) from None
            canonical = tuple(sorted(set(addresses)))
            if not canonical:
                raise ProviderDnsError(ProviderDnsErrorCode.EMPTY)
            if len(canonical) > MAXIMUM_EGRESS_ADDRESSES:
                raise ProviderDnsError(ProviderDnsErrorCode.LIMIT)
            return canonical
        finally:
            await _cancel_task(lookup_task)
            await _cancel_task(cancellation_task)


async def _system_lookup(hostname: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(str(record[4][0]) for record in records)


def _remaining_seconds(deadline_at: datetime) -> float:
    if (
        deadline_at.tzinfo is None
        or deadline_at.utcoffset() != timedelta(0)
    ):
        raise ValueError("provider DNS deadline must use UTC")
    remaining_seconds = (
        deadline_at - datetime.now(tz=deadline_at.tzinfo)
    ).total_seconds()
    if remaining_seconds <= 0:
        raise ProviderDnsError(ProviderDnsErrorCode.DEADLINE)
    return remaining_seconds


async def _cancel_task[ResultT](
    task: asyncio.Future[ResultT],
) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
