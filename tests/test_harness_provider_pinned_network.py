import asyncio
import ssl
from collections.abc import Iterable

import httpcore
import pytest

from app.services.harness.protocol import DataClassification
from app.services.harness.providers import (
    PinnedProviderNetworkBackend,
    ProviderEgressPolicy,
    authorize_egress_target,
    provider_destination_sha256,
)

DESTINATION_URL = "https://api.provider.example/v1"


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self) -> None:
        super().__init__(
            [b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"]
        )
        self.server_hostname: str | None = None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.server_hostname = server_hostname
        return self


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []
        self.stream = RecordingStream()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.calls.append((host, port, local_address))
        return self.stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise AssertionError("Unix sockets must never reach the backend")

    async def sleep(self, seconds: float) -> None:
        return None


def authorized_target():
    configured_policy = ProviderEgressPolicy(
        provider="configured-provider",
        destination_url=DESTINATION_URL,
        destination_sha256=provider_destination_sha256(
            DESTINATION_URL
        ),
        allowed_redirect_origins=(),
        accepted_classifications=(DataClassification.CONFIDENTIAL,),
        max_request_bytes=1_024,
        max_response_bytes=2_048,
    )
    return authorize_egress_target(
        configured_policy,
        target_url="https://api.provider.example/v1/responses",
        classification=DataClassification.CONFIDENTIAL,
        request_bytes=10,
        resolved_addresses=("1.1.1.1", "8.8.8.8"),
        redirect_count=0,
    )


def test_connection_uses_pinned_ip_and_original_tls_hostname() -> None:
    async def scenario() -> None:
        recording = RecordingBackend()
        pinned = PinnedProviderNetworkBackend(
            authorized_target(),
            selected_address="8.8.8.8",
            backend=recording,
        )
        pool = httpcore.AsyncConnectionPool(
            network_backend=pinned,
            retries=0,
            max_connections=1,
            max_keepalive_connections=0,
        )
        try:
            response = await pool.request(
                method="POST",
                url="https://api.provider.example/v1/responses",
                headers=[(b"host", b"api.provider.example")],
                content=b"{}",
                extensions={
                    "timeout": {
                        "connect": 1.0,
                        "read": 1.0,
                        "write": 1.0,
                        "pool": 1.0,
                    }
                },
            )
        finally:
            await pool.aclose()

        assert response.status == 200
        assert recording.calls == [("8.8.8.8", 443, None)]
        assert recording.stream.server_hostname == "api.provider.example"

    asyncio.run(scenario())


def test_backend_denies_unpinned_targets_local_bind_and_unix_sockets() -> None:
    async def scenario() -> None:
        pinned = PinnedProviderNetworkBackend(
            authorized_target(),
            backend=RecordingBackend(),
        )
        with pytest.raises(httpcore.ConnectError):
            await pinned.connect_tcp("evil.example", 443)
        with pytest.raises(httpcore.ConnectError):
            await pinned.connect_tcp("api.provider.example", 8443)
        with pytest.raises(httpcore.ConnectError):
            await pinned.connect_tcp(
                "api.provider.example",
                443,
                local_address="127.0.0.1",
            )
        with pytest.raises(httpcore.ConnectError):
            await pinned.connect_unix_socket("/tmp/provider.sock")

    asyncio.run(scenario())


def test_backend_rejects_an_address_outside_authorized_dns_evidence() -> None:
    with pytest.raises(ValueError, match="not authorized"):
        PinnedProviderNetworkBackend(
            authorized_target(),
            selected_address="9.9.9.9",
            backend=RecordingBackend(),
        )
