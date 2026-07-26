"""In-memory HTTP-core components for the provider egress drill."""

from __future__ import annotations

import ssl
from collections.abc import Iterable

import httpcore

from app.services.harness.providers.credential_material import CredentialLease


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, response: bytes) -> None:
        super().__init__([response])
        self.server_hostname: str | None = None
        self.writes: list[bytes] = []

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.server_hostname = server_hostname
        return self

    async def write(
        self,
        buffer: bytes,
        timeout: float | None = None,
    ) -> None:
        self.writes.append(buffer)


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        response_body = b'{"ok":true}'
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(response_body)}\r\n\r\n".encode()
            + response_body
        )
        self.stream = RecordingStream(response)
        self.tcp_calls: list[tuple[str, int, str | None]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.tcp_calls.append((host, port, local_address))
        return self.stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise AssertionError("provider drill must not use Unix sockets")

    async def sleep(self, seconds: float) -> None:
        return None


class BearerCredentialEncoder:
    def __init__(self) -> None:
        self.temporary_buffers: list[bytearray] = []

    def encode(
        self,
        credential: CredentialLease,
    ) -> tuple[bytes, bytearray]:
        value = bytearray(b"Bearer ")
        value.extend(credential.secret_view())
        self.temporary_buffers.append(value)
        return b"authorization", value


async def public_lookup(hostname: str, port: int) -> tuple[str, ...]:
    return ("1.1.1.1",)


async def private_lookup(hostname: str, port: int) -> tuple[str, ...]:
    return ("127.0.0.1",)
