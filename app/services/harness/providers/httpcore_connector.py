"""Pinned, bounded HTTP-core connector owned by the egress gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

import httpcore

from app.services.harness.providers.credential_material import (
    CredentialLease,
    zero_buffer,
)
from app.services.harness.providers.egress_contracts import (
    ProviderEgressRequest,
    ProviderEgressResponse,
    SafeEgressHeader,
)
from app.services.harness.providers.egress_policy import AuthorizedEgressTarget
from app.services.harness.providers.pinned_network import (
    PinnedProviderNetworkBackend,
)

MAXIMUM_RESPONSE_HEADERS = 32
SAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "location",
        "openai-request-id",
        "request-id",
        "retry-after",
        "x-request-id",
    }
)
REDIRECT_STATUSES = frozenset({307, 308})


class HttpCoreConnectorErrorCode(StrEnum):
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    PROTOCOL = "protocol"
    RESPONSE_SIZE = "response_size"


class HttpCoreConnectorError(RuntimeError):
    def __init__(self, code: HttpCoreConnectorErrorCode) -> None:
        super().__init__("provider HTTP connector failed")
        self.code = code


class ProviderCredentialHeaderEncoder(Protocol):
    def encode(
        self,
        credential: CredentialLease,
    ) -> tuple[bytes, bytearray]: ...


NetworkBackendFactory = Callable[
    [AuthorizedEgressTarget],
    httpcore.AsyncNetworkBackend,
]


class HttpCoreEgressConnector:
    def __init__(
        self,
        credential_encoder: ProviderCredentialHeaderEncoder,
        *,
        backend_factory: NetworkBackendFactory | None = None,
    ) -> None:
        self._credential_encoder = credential_encoder
        self._backend_factory = (
            backend_factory or _default_backend_factory
        )

    async def send(
        self,
        request: ProviderEgressRequest,
        target: AuthorizedEgressTarget,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
        max_response_bytes: int,
    ) -> ProviderEgressResponse:
        remaining_seconds = _remaining_seconds(deadline_at)
        if cancellation.is_set():
            raise HttpCoreConnectorError(
                HttpCoreConnectorErrorCode.CANCELLED
            )
        request_task = asyncio.create_task(
            self._send_once(
                request,
                target,
                credential,
                timeout_seconds=remaining_seconds,
                max_response_bytes=max_response_bytes,
                cancellation=cancellation,
            )
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (request_task, cancellation_task),
                timeout=remaining_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                await _cancel_task(request_task)
                raise HttpCoreConnectorError(
                    HttpCoreConnectorErrorCode.CANCELLED
                )
            if request_task not in done:
                await _cancel_task(request_task)
                raise HttpCoreConnectorError(
                    HttpCoreConnectorErrorCode.DEADLINE
                )
            return request_task.result()
        except HttpCoreConnectorError:
            raise
        except asyncio.CancelledError:
            await _cancel_task(request_task)
            raise
        except Exception:
            raise HttpCoreConnectorError(
                HttpCoreConnectorErrorCode.PROTOCOL
            ) from None
        finally:
            await _cancel_task(cancellation_task)

    async def _send_once(
        self,
        request: ProviderEgressRequest,
        target: AuthorizedEgressTarget,
        credential: CredentialLease | None,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        cancellation: asyncio.Event,
    ) -> ProviderEgressResponse:
        headers = [
            (header.name.encode("ascii"), header.value.encode("ascii"))
            for header in request.metadata.safe_headers
        ]
        headers.append(
            (b"content-type", request.metadata.content_type.encode("ascii"))
        )
        temporary_credential_header: bytearray | None = None
        if credential is not None:
            header_name, temporary_credential_header = (
                self._credential_encoder.encode(credential)
            )
            headers.append((header_name, bytes(temporary_credential_header)))
        pool = httpcore.AsyncConnectionPool(
            network_backend=self._backend_factory(target),
            retries=0,
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
        )
        try:
            async with pool.stream(
                method="POST",
                url=target.canonical_url,
                headers=headers,
                content=request.body(),
                extensions={
                    "timeout": {
                        "connect": timeout_seconds,
                        "read": timeout_seconds,
                        "write": timeout_seconds,
                        "pool": timeout_seconds,
                    }
                },
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_stream():
                    if cancellation.is_set():
                        raise HttpCoreConnectorError(
                            HttpCoreConnectorErrorCode.CANCELLED
                        )
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise HttpCoreConnectorError(
                            HttpCoreConnectorErrorCode.RESPONSE_SIZE
                        )
                safe_headers, redirect_url = _response_headers(
                    response.status,
                    response.headers,
                )
                return ProviderEgressResponse(
                    status=response.status,
                    headers=safe_headers,
                    body=bytes(body),
                    redirect_url=redirect_url,
                )
        finally:
            await pool.aclose()
            if temporary_credential_header is not None:
                zero_buffer(temporary_credential_header)


def _response_headers(
    status: int,
    headers: list[tuple[bytes, bytes]],
) -> tuple[tuple[SafeEgressHeader, ...], str | None]:
    safe_headers: list[SafeEgressHeader] = []
    redirect_url: str | None = None
    for raw_name, raw_value in headers:
        try:
            name = raw_name.decode("ascii").lower()
            value = raw_value.decode("ascii")
        except UnicodeDecodeError:
            continue
        if name not in SAFE_RESPONSE_HEADERS:
            continue
        if len(safe_headers) >= MAXIMUM_RESPONSE_HEADERS:
            raise HttpCoreConnectorError(
                HttpCoreConnectorErrorCode.PROTOCOL
            )
        safe_headers.append(SafeEgressHeader(name=name, value=value))
        if name == "location" and status in REDIRECT_STATUSES:
            redirect_url = value
    safe_headers.sort(key=_header_name)
    return tuple(safe_headers), redirect_url


def _remaining_seconds(deadline_at: datetime) -> float:
    now = datetime.now(tz=deadline_at.tzinfo)
    if (
        deadline_at.tzinfo is None
        or deadline_at.utcoffset() != timedelta(0)
    ):
        raise ValueError("provider connector deadline must use UTC")
    remaining_seconds = (deadline_at - now).total_seconds()
    if remaining_seconds <= 0:
        raise HttpCoreConnectorError(
            HttpCoreConnectorErrorCode.DEADLINE
        )
    return remaining_seconds


def _header_name(header: SafeEgressHeader) -> str:
    return header.name


async def _cancel_task(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _default_backend_factory(
    target: AuthorizedEgressTarget,
) -> httpcore.AsyncNetworkBackend:
    return PinnedProviderNetworkBackend(target)
