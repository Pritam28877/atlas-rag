"""Reusable in-memory HTTP-core network fixtures."""

import httpcore


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, response_parts: list[bytes]) -> None:
        super().__init__(response_parts)
        self.writes: list[bytes] = []

    async def write(
        self,
        buffer: bytes,
        timeout: float | None = None,
    ) -> None:
        del timeout
        self.writes.append(buffer)


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, response_parts: list[bytes]) -> None:
        self.stream = RecordingStream(response_parts)
        self.connections: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host,
        port,
        timeout=None,
        local_address=None,
        socket_options=None,
    ):
        del timeout, local_address, socket_options
        self.connections.append((host, port))
        return self.stream

    async def connect_unix_socket(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("unexpected Unix socket")

    async def sleep(self, seconds: float) -> None:
        del seconds


class Resolver:
    def __init__(self, addresses: tuple[str, ...] = ("1.1.1.1",)) -> None:
        self.addresses = addresses
        self.calls: list[tuple[str, int]] = []

    async def resolve(
        self,
        hostname,
        port,
        *,
        cancellation,
        deadline_at,
    ):
        del cancellation, deadline_at
        self.calls.append((hostname, port))
        return self.addresses


class RecordingCredentialEncoder:
    def __init__(self) -> None:
        self.buffers: list[bytearray] = []

    def encode(self, credential):
        value = bytearray(b"Bearer ")
        value.extend(credential.secret_view())
        self.buffers.append(value)
        return b"authorization", value


def sse_response(
    *,
    status: bytes = b"200 OK",
    content_type: bytes = b"text/event-stream; charset=utf-8",
    body: bytes = b"data: {}\n\n",
) -> bytes:
    return (
        b"HTTP/1.1 "
        + status
        + b"\r\nContent-Type: "
        + content_type
        + b"\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
