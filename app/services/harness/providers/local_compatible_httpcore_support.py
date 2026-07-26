"""Validation and cancellation helpers for local HTTP streaming."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator
from enum import StrEnum

from app.services.harness.providers.egress_policy import (
    MAXIMUM_EGRESS_ADDRESSES,
)


class LocalHttpCoreErrorCode(StrEnum):
    AUTHENTICATION = "authentication"
    CANCELLED = "cancelled"
    CONTENT_TYPE = "content_type"
    DEADLINE = "deadline"
    DESTINATION = "destination"
    HTTP_STATUS = "http_status"
    PROTOCOL = "protocol"
    REDIRECT = "redirect"
    RESOLUTION = "resolution"


class LocalHttpCoreError(RuntimeError):
    def __init__(self, code: LocalHttpCoreErrorCode) -> None:
        super().__init__("local-compatible HTTP connector failed")
        self.code = code


def public_addresses(addresses: tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(addresses) <= MAXIMUM_EGRESS_ADDRESSES:
        raise LocalHttpCoreError(LocalHttpCoreErrorCode.RESOLUTION)
    canonical: list[str] = []
    for address in addresses:
        if "%" in address:
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.RESOLUTION)
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise LocalHttpCoreError(
                LocalHttpCoreErrorCode.RESOLUTION
            ) from None
        if not parsed.is_global or parsed.is_multicast:
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.RESOLUTION)
        canonical.append(parsed.compressed)
    result = tuple(sorted(set(canonical)))
    if len(result) != len(addresses):
        raise LocalHttpCoreError(LocalHttpCoreErrorCode.RESOLUTION)
    return result


def validate_response(
    status: int,
    headers: list[tuple[bytes, bytes]],
) -> None:
    if status in {301, 302, 303, 307, 308}:
        raise LocalHttpCoreError(LocalHttpCoreErrorCode.REDIRECT)
    if not 200 <= status <= 299:
        raise LocalHttpCoreError(LocalHttpCoreErrorCode.HTTP_STATUS)
    content_types = [
        value
        for name, value in headers
        if name.lower() == b"content-type"
    ]
    if len(content_types) != 1:
        raise LocalHttpCoreError(LocalHttpCoreErrorCode.CONTENT_TYPE)
    media_type = content_types[0].split(b";", 1)[0].strip().lower()
    if media_type != b"text/event-stream":
        raise LocalHttpCoreError(LocalHttpCoreErrorCode.CONTENT_TYPE)


async def next_chunk(
    iterator: AsyncIterator[bytes],
    cancellation: asyncio.Event,
    timeout_seconds: float,
) -> bytes | None:
    if cancellation.is_set():
        raise LocalHttpCoreError(LocalHttpCoreErrorCode.CANCELLED)
    read_task: asyncio.Future[bytes] = asyncio.ensure_future(
        anext(iterator)
    )
    cancellation_task = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            (read_task, cancellation_task),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            await _cancel_task(read_task)
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.CANCELLED)
        if read_task not in done:
            await _cancel_task(read_task)
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.DEADLINE)
        try:
            return read_task.result()
        except StopAsyncIteration:
            return None
    finally:
        await _cancel_task(cancellation_task)


async def _cancel_task[FutureResult](
    task: asyncio.Future[FutureResult],
) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def timeouts(seconds: float) -> dict[str, float]:
    return {
        "connect": seconds,
        "read": seconds,
        "write": seconds,
        "pool": seconds,
    }
