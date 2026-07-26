"""Pinned HTTP-core connector for authorized regional Vertex streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from datetime import datetime

import httpcore

from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_contracts import (
    ProviderAddressResolver,
    ProviderEgressRequest,
)
from app.services.harness.providers.egress_policy import AuthorizedEgressTarget
from app.services.harness.providers.httpcore_connector import (
    ProviderCredentialHeaderEncoder,
)
from app.services.harness.providers.provider_sse_httpcore import (
    AuthorizedSseHttpRoute,
    PinnedSseHttpCoreConnector,
)
from app.services.harness.providers.vertex_policy import AuthorizedVertexRoute

NetworkBackendFactory = Callable[
    [AuthorizedEgressTarget],
    httpcore.AsyncNetworkBackend,
]


class VertexHttpCoreConnector:
    def __init__(
        self,
        resolver: ProviderAddressResolver,
        *,
        credential_encoder: ProviderCredentialHeaderEncoder | None = None,
        backend_factory: NetworkBackendFactory | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        self._delegate = PinnedSseHttpCoreConnector(
            resolver,
            credential_encoder=credential_encoder,
            backend_factory=backend_factory,
            clock=clock,
        )

    async def stream(
        self,
        request: ProviderEgressRequest,
        route: AuthorizedVertexRoute,
        credential: CredentialLease,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncGenerator[bytes, None]:
        generic_route = AuthorizedSseHttpRoute(
            provider="vertex",
            target_url=f"{route.stream_url}?alt=sse",
            destination_sha256=route.destination_sha256,
            credential_handle=route.credential_handle,
            credential_required=True,
            loopback_literal=False,
        )
        async for chunk in self._delegate.stream(
            request,
            generic_route,
            credential,
            cancellation=cancellation,
            deadline_at=deadline_at,
        ):
            yield chunk
