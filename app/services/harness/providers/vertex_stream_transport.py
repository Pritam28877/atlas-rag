"""Bounded Vertex GenerateContent transport over an authorized connector."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import datetime
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
from app.services.harness.providers.provider_stream_contracts import (
    ProviderStreamingBody,
)
from app.services.harness.providers.provider_stream_control import (
    ProviderStreamControlError,
    ProviderStreamControlErrorCode,
    acquire_stream_capacity,
    remaining_seconds,
)
from app.services.harness.providers.sse_framer import BoundedSseFramer
from app.services.harness.providers.vertex_contracts import (
    CompiledVertexGenerateContentRequest,
)
from app.services.harness.providers.vertex_decoder import (
    VertexGenerateContentDecoder,
)
from app.services.harness.providers.vertex_policy import AuthorizedVertexRoute


class VertexStreamingConnector(Protocol):
    def stream(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedVertexRoute,
        credential: CredentialLease,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderStreamingBody: ...


class VertexTransportErrorCode(StrEnum):
    CANCELLED = "cancelled"
    CLOSED = "closed"
    DEADLINE = "deadline"
    MODEL = "model"
    PROVIDER = "provider"
    STREAM = "stream"


class VertexTransportError(RuntimeError):
    def __init__(self, code: VertexTransportErrorCode) -> None:
        super().__init__("Vertex GenerateContent transport failed")
        self.code = code


class BoundedVertexGenerateContentTransport:
    def __init__(
        self,
        connector: VertexStreamingConnector,
        *,
        clock: Callable[[], datetime],
        maximum_concurrent_streams: int = 4,
    ) -> None:
        if not 1 <= maximum_concurrent_streams <= 16:
            raise ValueError("Vertex stream concurrency is invalid")
        self._connector = connector
        self._clock = clock
        self._capacity = asyncio.Semaphore(maximum_concurrent_streams)
        self._closed = False

    async def stream(
        self,
        canonical_request: CanonicalProviderRequest,
        compiled: CompiledVertexGenerateContentRequest,
        route: AuthorizedVertexRoute,
        credential: CredentialLease,
        decoder: VertexGenerateContentDecoder,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if self._closed:
            self._reject(VertexTransportErrorCode.CLOSED)
        if compiled.model != route.model_id:
            self._reject(VertexTransportErrorCode.MODEL)
        request = _egress_request(canonical_request, compiled, route)
        try:
            remaining = remaining_seconds(self._clock, deadline_at)
            await acquire_stream_capacity(
                self._capacity,
                cancellation,
                remaining,
            )
        except ProviderStreamControlError as error:
            self._reject(_control_error_code(error))
        framer = BoundedSseFramer()
        terminal = False
        try:
            response = self._connector.stream(
                request,
                route,
                credential,
                cancellation=cancellation,
                deadline_at=deadline_at,
            )
            async with aclosing(response):
                async for chunk in response:
                    if cancellation.is_set():
                        yield decoder.cancel()
                        return
                    try:
                        remaining_seconds(self._clock, deadline_at)
                    except ProviderStreamControlError:
                        self._reject(VertexTransportErrorCode.DEADLINE)
                    for record in framer.feed(chunk):
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
        except VertexTransportError:
            raise
        except Exception:
            if cancellation.is_set() and not terminal:
                yield decoder.cancel()
                return
            self._reject(VertexTransportErrorCode.PROVIDER)
        finally:
            self._capacity.release()
        if not terminal:
            self._reject(VertexTransportErrorCode.STREAM)

    async def close(self) -> None:
        self._closed = True

    @staticmethod
    def _reject(code: VertexTransportErrorCode) -> None:
        raise VertexTransportError(code)


def _egress_request(
    request: CanonicalProviderRequest,
    compiled: CompiledVertexGenerateContentRequest,
    route: AuthorizedVertexRoute,
) -> ProviderEgressRequest:
    body = json.dumps(
        compiled.to_wire(),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return ProviderEgressRequest(
        request_id=request.request_id,
        provider="vertex",
        credential_handle=route.credential_handle,
        target_url=f"{route.stream_url}?alt=sse",
        classification=request.classification,
        content_type="application/json",
        safe_headers=(
            SafeEgressHeader(name="accept", value="text/event-stream"),
            SafeEgressHeader(
                name="x-goog-user-project",
                value=route.project_id,
            ),
        ),
        body=body,
    )


def _control_error_code(
    error: ProviderStreamControlError,
) -> VertexTransportErrorCode:
    if error.code is ProviderStreamControlErrorCode.CANCELLED:
        return VertexTransportErrorCode.CANCELLED
    return VertexTransportErrorCode.DEADLINE
