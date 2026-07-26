"""Bounded local-compatible Responses transport over an authorized connector."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from app.services.harness.protocol import (
    CanonicalProviderRequest,
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderStreamEvent,
)
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import (
    ProviderEgressRequest,
    SafeEgressHeader,
)
from app.services.harness.providers.local_compatible_policy import (
    AuthorizedLocalCompatibleRoute,
)
from app.services.harness.providers.local_compatible_sse import BoundedSseFramer
from app.services.harness.providers.openai_contracts import (
    CompiledOpenAIResponsesRequest,
)
from app.services.harness.providers.openai_decoder import OpenAIResponsesDecoder


class LocalStreamingConnector(Protocol):
    def stream(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedLocalCompatibleRoute,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> LocalStreamingBody: ...


class LocalStreamingBody(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def __anext__(self) -> bytes: ...

    async def aclose(self) -> None: ...


class LocalCompatibleTransportErrorCode(StrEnum):
    CANCELLED = "cancelled"
    CLOSED = "closed"
    DEADLINE = "deadline"
    MODEL = "model"
    PROVIDER = "provider"
    STREAM = "stream"


class LocalCompatibleTransportError(RuntimeError):
    def __init__(self, code: LocalCompatibleTransportErrorCode) -> None:
        super().__init__("local-compatible Responses transport failed")
        self.code = code


class BoundedLocalCompatibleResponsesTransport:
    def __init__(
        self,
        connector: LocalStreamingConnector,
        *,
        clock: Callable[[], datetime],
        maximum_concurrent_streams: int = 4,
    ) -> None:
        if not 1 <= maximum_concurrent_streams <= 16:
            raise ValueError("local-compatible stream concurrency is invalid")
        self._connector = connector
        self._clock = clock
        self._capacity = asyncio.Semaphore(maximum_concurrent_streams)
        self._closed = False

    async def stream(
        self,
        canonical_request: CanonicalProviderRequest,
        compiled: CompiledOpenAIResponsesRequest,
        route: AuthorizedLocalCompatibleRoute,
        credential: CredentialLease | None,
        decoder: OpenAIResponsesDecoder,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if self._closed:
            self._reject(LocalCompatibleTransportErrorCode.CLOSED)
        if compiled.model != route.model_id:
            self._reject(LocalCompatibleTransportErrorCode.MODEL)
        remaining = self._remaining(deadline_at)
        await _acquire_capacity(self._capacity, cancellation, remaining)
        request = _egress_request(canonical_request, compiled, route)
        framer = BoundedSseFramer()
        terminal = False
        try:
            stream = self._connector.stream(
                request,
                route,
                credential,
                cancellation=cancellation,
                deadline_at=deadline_at,
            )
            async with aclosing(stream):
                async for chunk in stream:
                    if cancellation.is_set():
                        yield decoder.cancel()
                        return
                    self._remaining(deadline_at)
                    for record in framer.feed(chunk):
                        if record == b"[DONE]":
                            if not terminal:
                                self._reject(
                                    LocalCompatibleTransportErrorCode.STREAM
                                )
                            continue
                        for event in decoder.decode(record):
                            yield event
                            terminal = isinstance(
                                event,
                                (
                                    ProviderCancelled,
                                    ProviderCompleted,
                                    ProviderError,
                                ),
                            )
                            if cancellation.is_set() and not terminal:
                                yield decoder.cancel()
                                return
            framer.finish()
        except LocalCompatibleTransportError:
            raise
        except Exception:
            if cancellation.is_set() and not terminal:
                yield decoder.cancel()
                return
            self._reject(LocalCompatibleTransportErrorCode.PROVIDER)
        finally:
            self._capacity.release()
        if not terminal:
            self._reject(LocalCompatibleTransportErrorCode.STREAM)

    async def close(self) -> None:
        self._closed = True

    def _remaining(self, deadline_at: datetime) -> float:
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timedelta(0)
            or deadline_at <= now
        ):
            self._reject(LocalCompatibleTransportErrorCode.DEADLINE)
        return (deadline_at - now).total_seconds()

    @staticmethod
    def _reject(code: LocalCompatibleTransportErrorCode) -> None:
        raise LocalCompatibleTransportError(code)


def _egress_request(
    request: CanonicalProviderRequest,
    compiled: CompiledOpenAIResponsesRequest,
    route: AuthorizedLocalCompatibleRoute,
) -> ProviderEgressRequest:
    body = compiled.model_dump_json(exclude_none=True).encode()
    return ProviderEgressRequest(
        request_id=request.request_id,
        provider="local-compatible",
        credential_handle=route.credential_handle,
        target_url=route.responses_url,
        classification=request.classification,
        content_type="application/json",
        safe_headers=(
            SafeEgressHeader(name="accept", value="text/event-stream"),
        ),
        body=body,
    )


async def _acquire_capacity(
    capacity: asyncio.Semaphore,
    cancellation: asyncio.Event,
    timeout_seconds: float,
) -> None:
    if cancellation.is_set():
        raise LocalCompatibleTransportError(
            LocalCompatibleTransportErrorCode.CANCELLED
        )
    acquire_task = asyncio.create_task(capacity.acquire())
    cancellation_task = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            (acquire_task, cancellation_task),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            await _cancel_task(acquire_task)
            raise LocalCompatibleTransportError(
                LocalCompatibleTransportErrorCode.CANCELLED
            )
        if acquire_task not in done:
            await _cancel_task(acquire_task)
            raise LocalCompatibleTransportError(
                LocalCompatibleTransportErrorCode.DEADLINE
            )
        acquire_task.result()
    finally:
        await _cancel_task(cancellation_task)


async def _cancel_task(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
