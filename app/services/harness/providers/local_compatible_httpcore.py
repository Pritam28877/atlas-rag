"""Pinned HTTP-core streaming connector for authorized local endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncGenerator, Callable
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpcore

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
from app.services.harness.providers.egress_policy import (
    AuthorizedEgressTarget,
)
from app.services.harness.providers.httpcore_connector import (
    ProviderCredentialHeaderEncoder,
)
from app.services.harness.providers.local_compatible_httpcore_support import (
    LocalHttpCoreError,
    LocalHttpCoreErrorCode,
    next_chunk,
    public_addresses,
    timeouts,
    validate_response,
)
from app.services.harness.providers.local_compatible_identity import (
    LocalAuthenticationMode,
)
from app.services.harness.providers.local_compatible_policy import (
    AuthorizedLocalCompatibleRoute,
    LocalEndpointMode,
)
from app.services.harness.providers.pinned_network import (
    PinnedProviderNetworkBackend,
)

NetworkBackendFactory = Callable[
    [AuthorizedEgressTarget],
    httpcore.AsyncNetworkBackend,
]


class LocalCompatibleHttpCoreConnector:
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
        route: AuthorizedLocalCompatibleRoute,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncGenerator[bytes, None]:
        self._validate_request(request, route, credential)
        if cancellation.is_set():
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.CANCELLED)
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
        temporary_credential: bytearray | None = None
        if credential is not None:
            header_name, temporary_credential = self._credential_encoder.encode(
                credential
            )
            headers.append((header_name, bytes(temporary_credential)))
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
        except LocalHttpCoreError:
            raise
        except Exception:
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.PROTOCOL) from None
        finally:
            await pool.aclose()
            if temporary_credential is not None:
                zero_buffer(temporary_credential)

    def _validate_request(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedLocalCompatibleRoute,
        credential: CredentialLease | None,
    ) -> None:
        expected_credential = (
            route.authentication
            is LocalAuthenticationMode.BEARER_ENVIRONMENT
        )
        if expected_credential != (credential is not None):
            raise LocalHttpCoreError(
                LocalHttpCoreErrorCode.AUTHENTICATION
            )
        if (
            request.metadata.provider != "local-compatible"
            or request.metadata.target_url != route.responses_url
            or request.metadata.credential_handle != route.credential_handle
        ):
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.DESTINATION)
        if credential is not None and (
            credential.handle != route.credential_handle
            or credential.provider != "local-compatible"
            or credential.destination_sha256 != route.destination_sha256
        ):
            raise LocalHttpCoreError(
                LocalHttpCoreErrorCode.AUTHENTICATION
            )

    async def _target(
        self,
        route: AuthorizedLocalCompatibleRoute,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AuthorizedEgressTarget:
        parts = urlsplit(route.responses_url)
        hostname = parts.hostname
        if hostname is None:
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.DESTINATION)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses: tuple[str, ...]
        if route.endpoint_mode is LocalEndpointMode.LOOPBACK:
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                raise LocalHttpCoreError(
                    LocalHttpCoreErrorCode.DESTINATION
                ) from None
            if not address.is_loopback:
                raise LocalHttpCoreError(
                    LocalHttpCoreErrorCode.DESTINATION
                )
            addresses = (address.compressed,)
        else:
            try:
                resolved = await self._resolver.resolve(
                    hostname,
                    port,
                    cancellation=cancellation,
                    deadline_at=deadline_at,
                )
                addresses = public_addresses(resolved)
            except LocalHttpCoreError:
                raise
            except Exception:
                raise LocalHttpCoreError(
                    LocalHttpCoreErrorCode.RESOLUTION
                ) from None
        return AuthorizedEgressTarget(
            provider="local-compatible",
            canonical_url=route.responses_url,
            destination_sha256=route.destination_sha256,
            hostname=hostname,
            port=port,
            resolved_addresses=addresses,
            redirect_count=0,
        )

    def _remaining(self, deadline_at: datetime) -> float:
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timedelta(0)
            or deadline_at <= now
        ):
            raise LocalHttpCoreError(LocalHttpCoreErrorCode.DEADLINE)
        return (deadline_at - now).total_seconds()


def _default_backend_factory(
    target: AuthorizedEgressTarget,
) -> httpcore.AsyncNetworkBackend:
    return PinnedProviderNetworkBackend(target)
