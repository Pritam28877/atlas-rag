"""Pinned HTTP-core connector shared by authorized SSE providers."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpcore

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    ProviderName,
    Sha256,
)
from app.services.harness.providers.bearer_authorization import (
    BearerProviderCredentialEncoder,
)
from app.services.harness.providers.credential_material import (
    CredentialLease,
    zero_buffer,
)
from app.services.harness.providers.egress_contracts import (
    ProviderAddressResolver,
    ProviderEgressRequest,
)
from app.services.harness.providers.egress_policy import AuthorizedEgressTarget
from app.services.harness.providers.httpcore_connector import (
    ProviderCredentialHeaderEncoder,
)
from app.services.harness.providers.pinned_network import (
    PinnedProviderNetworkBackend,
)
from app.services.harness.providers.provider_sse_httpcore_support import (
    SseHttpCoreError,
    SseHttpCoreErrorCode,
    next_chunk,
    public_addresses,
    timeouts,
    validate_response,
)

NetworkBackendFactory = Callable[
    [AuthorizedEgressTarget],
    httpcore.AsyncNetworkBackend,
]


@dataclass(frozen=True, slots=True)
class AuthorizedSseHttpRoute:
    provider: ProviderName
    target_url: str
    destination_sha256: Sha256
    credential_handle: ProviderCredentialHandle
    credential_required: bool
    loopback_literal: bool


class PinnedSseHttpCoreConnector:
    def __init__(
        self,
        resolver: ProviderAddressResolver,
        *,
        credential_encoder: ProviderCredentialHeaderEncoder | None = None,
        backend_factory: NetworkBackendFactory | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        self._resolver = resolver
        self._credential_encoder = (
            credential_encoder or BearerProviderCredentialEncoder()
        )
        self._backend_factory = (
            backend_factory or _default_backend_factory
        )
        self._clock = clock

    async def stream(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedSseHttpRoute,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncGenerator[bytes, None]:
        self._validate_request(request, route, credential)
        if cancellation.is_set():
            raise SseHttpCoreError(SseHttpCoreErrorCode.CANCELLED)
        target = await self._target(
            route,
            cancellation=cancellation,
            deadline_at=deadline_at,
        )
        remaining = self._remaining(deadline_at)
        headers = [
            (header.name.encode("ascii"), header.value.encode("ascii"))
            for header in request.metadata.safe_headers
        ]
        headers.append(
            (b"content-type", request.metadata.content_type.encode("ascii"))
        )
        pool = httpcore.AsyncConnectionPool(
            network_backend=self._backend_factory(target),
            retries=0,
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
        )
        temporary_credential: bytearray | None = None
        try:
            if credential is not None:
                (
                    header_name,
                    temporary_credential,
                ) = self._credential_encoder.encode(credential)
                headers.append(
                    (header_name, bytes(temporary_credential))
                )
            async with pool.stream(
                method="POST",
                url=target.canonical_url,
                headers=headers,
                content=request.body(),
                extensions={"timeout": timeouts(remaining)},
            ) as response:
                validate_response(response.status, response.headers)
                iterator = response.aiter_stream()
                while True:
                    chunk = await next_chunk(
                        iterator,
                        cancellation,
                        self._remaining(deadline_at),
                    )
                    if chunk is None:
                        return
                    if chunk:
                        yield chunk
        except SseHttpCoreError:
            raise
        except Exception:
            raise SseHttpCoreError(SseHttpCoreErrorCode.PROTOCOL) from None
        finally:
            try:
                await pool.aclose()
            finally:
                if temporary_credential is not None:
                    zero_buffer(temporary_credential)

    def _validate_request(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedSseHttpRoute,
        credential: CredentialLease | None,
    ) -> None:
        if route.credential_required != (credential is not None):
            raise SseHttpCoreError(SseHttpCoreErrorCode.AUTHENTICATION)
        if (
            request.metadata.provider != route.provider
            or request.metadata.target_url != route.target_url
            or request.metadata.credential_handle != route.credential_handle
        ):
            raise SseHttpCoreError(SseHttpCoreErrorCode.DESTINATION)
        now = self._clock()
        clock_is_valid = (
            now.tzinfo is not None
            and now.utcoffset() == timedelta(0)
        )
        if credential is not None and (
            credential.handle != route.credential_handle
            or credential.provider != route.provider
            or credential.destination_sha256 != route.destination_sha256
            or credential.released
            or not clock_is_valid
            or credential.expires_at <= now
        ):
            raise SseHttpCoreError(SseHttpCoreErrorCode.AUTHENTICATION)

    async def _target(
        self,
        route: AuthorizedSseHttpRoute,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AuthorizedEgressTarget:
        parts = urlsplit(route.target_url)
        hostname = parts.hostname
        if (
            hostname is None
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            raise SseHttpCoreError(SseHttpCoreErrorCode.DESTINATION)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses: tuple[str, ...]
        if route.loopback_literal:
            addresses = self._loopback_target(parts.scheme, hostname)
        else:
            if parts.scheme != "https":
                raise SseHttpCoreError(SseHttpCoreErrorCode.DESTINATION)
            try:
                resolved = await self._resolver.resolve(
                    hostname,
                    port,
                    cancellation=cancellation,
                    deadline_at=deadline_at,
                )
                addresses = public_addresses(resolved)
            except SseHttpCoreError:
                raise
            except Exception:
                raise SseHttpCoreError(
                    SseHttpCoreErrorCode.RESOLUTION
                ) from None
        return AuthorizedEgressTarget(
            provider=route.provider,
            canonical_url=route.target_url,
            destination_sha256=route.destination_sha256,
            hostname=hostname,
            port=port,
            resolved_addresses=addresses,
            redirect_count=0,
        )

    @staticmethod
    def _loopback_target(
        scheme: str,
        hostname: str,
    ) -> tuple[str, ...]:
        if scheme not in {"http", "https"}:
            raise SseHttpCoreError(SseHttpCoreErrorCode.DESTINATION)
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            raise SseHttpCoreError(
                SseHttpCoreErrorCode.DESTINATION
            ) from None
        if not address.is_loopback:
            raise SseHttpCoreError(SseHttpCoreErrorCode.DESTINATION)
        return (address.compressed,)

    def _remaining(self, deadline_at: datetime) -> float:
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timedelta(0)
            or deadline_at <= now
        ):
            raise SseHttpCoreError(SseHttpCoreErrorCode.DEADLINE)
        return (deadline_at - now).total_seconds()


def _default_backend_factory(
    target: AuthorizedEgressTarget,
) -> httpcore.AsyncNetworkBackend:
    return PinnedProviderNetworkBackend(target)
