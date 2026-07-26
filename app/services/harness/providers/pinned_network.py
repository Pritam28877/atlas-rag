"""HTTP-core network backend pinned to one authorized provider address."""

from __future__ import annotations

from collections.abc import Iterable

import httpcore

from app.services.harness.providers.egress_policy import (
    AuthorizedEgressTarget,
)


class PinnedProviderNetworkBackend(httpcore.AsyncNetworkBackend):
    """Prevents a second DNS lookup while retaining hostname-based TLS."""

    def __init__(
        self,
        target: AuthorizedEgressTarget,
        *,
        selected_address: str | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        address = selected_address or target.resolved_addresses[0]
        if address not in target.resolved_addresses:
            raise ValueError("selected provider address was not authorized")
        self._hostname = target.hostname
        self._port = target.port
        self._address = address
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (
            host != self._hostname
            or port != self._port
            or local_address is not None
        ):
            raise httpcore.ConnectError(
                "provider pinned network connection denied"
            )
        return await self._backend.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=None,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError(
            "provider Unix socket connection denied"
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)
