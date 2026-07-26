import asyncio
import os
import stat
from pathlib import Path

import pytest

from app.services.harness.runtime import (
    HandshakeChallenge,
    HandshakeProof,
    LocalStreamRunner,
    PeerSessionTokenManager,
    UnixChallengeHandshake,
    UnixHarnessServer,
    UnixPeerCredentialReader,
    encode_frame,
)

HANDSHAKE_BYTES = 4_096


class Connection:
    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.closed = False
        self.received_event = asyncio.Event()

    async def receive(self, chunk: bytes, sender: object) -> None:
        self.received.append(chunk)
        self.received_event.set()

    async def close(self) -> None:
        self.closed = True


class ConnectionFactory:
    def __init__(self) -> None:
        self.connections: list[Connection] = []

    def create(self, session: object) -> Connection:
        connection = Connection()
        self.connections.append(connection)
        return connection


def private_socket_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "atlas.sock"


def server(
    socket_path: Path,
    factory: ConnectionFactory,
    *,
    maximum_sessions: int = 8,
    handshake_timeout: float = 1,
) -> UnixHarnessServer:
    return UnixHarnessServer(
        socket_path=socket_path,
        handshake=UnixChallengeHandshake(
            UnixPeerCredentialReader(),
            PeerSessionTokenManager(b"k" * 32),
            timeout_seconds=handshake_timeout,
        ),
        connection_factory=factory,  # type: ignore[arg-type]
        stream_runner=LocalStreamRunner(read_chunk_bytes=64 * 1024),
        maximum_sessions=maximum_sessions,
    )


async def authenticate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    header = await reader.readexactly(4)
    payload = await reader.readexactly(int.from_bytes(header, "big"))
    challenge = HandshakeChallenge.model_validate_json(payload)
    proof = HandshakeProof(token=challenge.token)
    writer.write(
        encode_frame(
            proof.model_dump_json().encode(),
            maximum_frame_bytes=HANDSHAKE_BYTES,
        )
    )
    await writer.drain()


def test_real_unix_listener_authenticates_and_owns_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = private_socket_path(tmp_path)
        factory = ConnectionFactory()
        harness_server = server(socket_path, factory)
        await harness_server.start()
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600

        reader, writer = await asyncio.open_unix_connection(socket_path)
        await authenticate(reader, writer)
        writer.write(b"command-frame")
        await writer.drain()
        while not factory.connections:
            await asyncio.sleep(0)
        connection = factory.connections[0]
        await asyncio.wait_for(connection.received_event.wait(), timeout=1)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0)
        await harness_server.close()

        assert connection.received == [b"command-frame"]
        assert connection.closed
        assert not socket_path.exists()
        assert harness_server.snapshot().active_sessions == 0

    asyncio.run(scenario())


def test_capacity_rejects_without_a_waiting_session_queue(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = private_socket_path(tmp_path)
        factory = ConnectionFactory()
        harness_server = server(
            socket_path,
            factory,
            maximum_sessions=1,
            handshake_timeout=5,
        )
        await harness_server.start()
        first_reader, first_writer = await asyncio.open_unix_connection(
            socket_path
        )
        await first_reader.readexactly(4)

        second_reader, second_writer = await asyncio.open_unix_connection(
            socket_path
        )
        assert await asyncio.wait_for(second_reader.read(), timeout=1) == b""
        snapshot = harness_server.snapshot()
        assert snapshot.active_sessions == 1
        assert snapshot.capacity_rejections == 1

        first_writer.close()
        second_writer.close()
        await harness_server.close()

    asyncio.run(scenario())


def test_shutdown_cancels_incomplete_handshake_and_removes_socket(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket_path = private_socket_path(tmp_path)
        harness_server = server(
            socket_path,
            ConnectionFactory(),
            handshake_timeout=5,
        )
        await harness_server.start()
        reader, writer = await asyncio.open_unix_connection(socket_path)
        await reader.readexactly(4)

        await harness_server.close()

        assert not socket_path.exists()
        assert harness_server.snapshot().active_sessions == 0
        writer.close()

    asyncio.run(scenario())


def test_invalid_proof_is_rejected_and_counted_without_detail(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket_path = private_socket_path(tmp_path)
        harness_server = server(socket_path, ConnectionFactory())
        await harness_server.start()
        reader, writer = await asyncio.open_unix_connection(socket_path)
        header = await reader.readexactly(4)
        await reader.readexactly(int.from_bytes(header, "big"))
        writer.write(
            encode_frame(
                b'{"kind":"peer_proof","token":"invalid"}',
                maximum_frame_bytes=HANDSHAKE_BYTES,
            )
        )
        await writer.drain()

        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        assert harness_server.snapshot().authentication_failures == 1
        await harness_server.close()

    asyncio.run(scenario())


def test_existing_path_and_insecure_parent_are_never_modified(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket_path = private_socket_path(tmp_path)
        socket_path.write_text("owned by user", encoding="utf-8")
        harness_server = server(socket_path, ConnectionFactory())
        with pytest.raises(FileExistsError):
            await harness_server.start()
        assert socket_path.read_text(encoding="utf-8") == "owned by user"

        socket_path.unlink()
        os.chmod(tmp_path, 0o755)
        with pytest.raises(PermissionError):
            await harness_server.start()
        assert not socket_path.exists()

    asyncio.run(scenario())


def test_replaced_socket_path_is_not_unlinked_on_shutdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = private_socket_path(tmp_path)
        harness_server = server(socket_path, ConnectionFactory())
        await harness_server.start()
        socket_path.unlink()
        socket_path.write_text("replacement", encoding="utf-8")

        await harness_server.close()

        assert socket_path.read_text(encoding="utf-8") == "replacement"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("maximum_sessions", "stream_limit_bytes"),
    ((0, 4096), (65, 4096), (1, 4095), (1, 4 * 1024 * 1024 + 5)),
)
def test_listener_limits_are_hard_bounded(
    tmp_path: Path,
    maximum_sessions: int,
    stream_limit_bytes: int,
) -> None:
    socket_path = private_socket_path(tmp_path)
    with pytest.raises(ValueError):
        UnixHarnessServer(
            socket_path=socket_path,
            handshake=UnixChallengeHandshake(
                UnixPeerCredentialReader(),
                PeerSessionTokenManager(b"k" * 32),
            ),
            connection_factory=ConnectionFactory(),  # type: ignore[arg-type]
            stream_runner=LocalStreamRunner(read_chunk_bytes=4096),
            maximum_sessions=maximum_sessions,
            stream_limit_bytes=stream_limit_bytes,
        )
