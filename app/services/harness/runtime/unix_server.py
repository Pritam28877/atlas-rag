"""Private bounded Unix listener for authenticated local harness clients."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
from pathlib import Path
from typing import Protocol, cast

from pydantic import Field

from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.runtime.connection import LocalConnection
from app.services.harness.runtime.handshake import (
    UnixChallengeHandshake,
    UnixHandshakeError,
)
from app.services.harness.runtime.peer_auth import VerifiedPeerSession
from app.services.harness.runtime.streams import LocalStreamRunner


class LocalConnectionFactory(Protocol):
    def create(self, session: VerifiedPeerSession) -> LocalConnection: ...


class UnixServerSnapshot(StrictProtocolModel):
    running: bool
    active_sessions: int = Field(ge=0, le=64)
    capacity_rejections: int = Field(ge=0)
    authentication_failures: int = Field(ge=0)
    connection_failures: int = Field(ge=0)


class UnixHarnessServer:
    """Owns one private socket path and every accepted connection task."""

    def __init__(
        self,
        *,
        socket_path: Path,
        handshake: UnixChallengeHandshake,
        connection_factory: LocalConnectionFactory,
        stream_runner: LocalStreamRunner,
        maximum_sessions: int = 8,
        stream_limit_bytes: int = 1024 * 1024 + 4,
        shutdown_timeout_seconds: float = 5,
    ) -> None:
        if not 1 <= maximum_sessions <= 64:
            raise ValueError("maximum_sessions must be between 1 and 64")
        if not 4_096 <= stream_limit_bytes <= 4 * 1024 * 1024 + 4:
            raise ValueError("stream_limit_bytes must be between 4 KiB and 4 MiB + 4")
        if not 0.001 <= shutdown_timeout_seconds <= 30:
            raise ValueError(
                "shutdown_timeout_seconds must be between 1 ms and 30 seconds"
            )
        self._socket_path = socket_path
        self._handshake = handshake
        self._connection_factory = connection_factory
        self._stream_runner = stream_runner
        self._maximum_sessions = maximum_sessions
        self._stream_limit_bytes = stream_limit_bytes
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._client_tasks: set[asyncio.Task[object]] = set()
        self._active_sessions = 0
        self._capacity_rejections = 0
        self._authentication_failures = 0
        self._connection_failures = 0

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("Unix harness server is already running")
        self._validate_socket_location()
        try:
            self._server = await asyncio.start_unix_server(
                self._serve_client,
                path=self._socket_path,
                limit=self._stream_limit_bytes,
            )
            socket_status = self._socket_path.lstat()
            if not stat.S_ISSOCK(socket_status.st_mode):
                raise RuntimeError("Unix listener path is not a socket")
            self._socket_identity = (
                socket_status.st_dev,
                socket_status.st_ino,
            )
            os.chmod(self._socket_path, 0o600)
        except BaseException:
            await self._close_listener()
            self._unlink_owned_socket()
            raise

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        try:
            tasks = tuple(self._client_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                async with asyncio.timeout(self._shutdown_timeout_seconds):
                    await asyncio.gather(*tasks, return_exceptions=True)
            if server is not None:
                await server.wait_closed()
        finally:
            self._unlink_owned_socket()

    def snapshot(self) -> UnixServerSnapshot:
        return UnixServerSnapshot(
            running=self._server is not None,
            active_sessions=self._active_sessions,
            capacity_rejections=self._capacity_rejections,
            authentication_failures=self._authentication_failures,
            connection_failures=self._connection_failures,
        )

    async def _serve_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is None:
            writer.close()
            return
        if self._server is None:
            writer.close()
            return
        self._client_tasks.add(task)
        if self._active_sessions >= self._maximum_sessions:
            self._capacity_rejections += 1
            await self._close_writer(writer)
            self._client_tasks.discard(task)
            return
        self._active_sessions += 1
        try:
            peer_socket = cast(
                socket.socket | None,
                writer.get_extra_info("socket"),
            )
            if peer_socket is None or peer_socket.family != socket.AF_UNIX:
                raise RuntimeError("Unix stream omitted peer socket")
            session = await self._handshake.authenticate(
                reader,
                writer,
                peer_socket,
            )
            connection = self._connection_factory.create(session)
            await self._stream_runner.serve(reader, writer, connection)
        except UnixHandshakeError:
            self._authentication_failures += 1
            await self._close_writer(writer)
        except asyncio.CancelledError:
            writer.close()
            raise
        except Exception:
            self._connection_failures += 1
            await self._close_writer(writer)
        finally:
            self._active_sessions -= 1
            self._client_tasks.discard(task)

    async def _close_listener(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                await writer.wait_closed()
        except (ConnectionError, TimeoutError):
            return

    def _validate_socket_location(self) -> None:
        if not self._socket_path.is_absolute():
            raise ValueError("Unix socket path must be absolute")
        if len(os.fsencode(self._socket_path)) > 107:
            raise ValueError("Unix socket path exceeds platform limit")
        parent = self._socket_path.parent
        resolved_parent = parent.resolve(strict=True)
        if resolved_parent != parent:
            raise ValueError("Unix socket parent must be canonical")
        parent_status = parent.stat()
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or parent_status.st_uid != os.getuid()
            or stat.S_IMODE(parent_status.st_mode) & 0o077
        ):
            raise PermissionError(
                "Unix socket parent must be owner-only and owner-managed"
            )
        if os.path.lexists(self._socket_path):
            raise FileExistsError("Unix socket path already exists")

    def _unlink_owned_socket(self) -> None:
        identity = self._socket_identity
        self._socket_identity = None
        if identity is None:
            return
        try:
            socket_status = self._socket_path.lstat()
        except FileNotFoundError:
            return
        current_identity = (socket_status.st_dev, socket_status.st_ino)
        if current_identity == identity and stat.S_ISSOCK(socket_status.st_mode):
            self._socket_path.unlink()
