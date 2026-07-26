"""Injected local Atlas Harness app-server composition."""

from __future__ import annotations

import asyncio
import secrets
from typing import Literal

from pydantic import Field

from app.core.harness_config import HarnessSettings
from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.runtime import (
    AsyncStreamWriter,
    AuthorityRepository,
    CommandAuthorityBinder,
    CommandDispatcher,
    LocalConnection,
    LocalStreamRunner,
    PeerSessionTokenManager,
    RequestAdmission,
    StdioParentCredentialReader,
    StdioSessionAuthenticator,
    UnixChallengeHandshake,
    UnixHarnessServer,
    UnixPeerCredentialReader,
    VerifiedPeerSession,
)


class LocalAppServerSnapshot(StrictProtocolModel):
    transport: Literal["stdio", "unix"]
    unix_listener_running: bool
    stdio_session_active: bool
    active_unix_sessions: int = Field(ge=0, le=64)


class HarnessConnectionFactory:
    """Creates connections that share bounded admission and authority storage."""

    def __init__(
        self,
        settings: HarnessSettings,
        authority_repository: AuthorityRepository,
        dispatcher: CommandDispatcher,
    ) -> None:
        self._settings = settings
        self._authority = CommandAuthorityBinder(authority_repository)
        self._dispatcher = dispatcher
        self._admission = RequestAdmission(
            maximum_concurrency=settings.active_sessions,
            maximum_request_seconds=settings.request_timeout_seconds,
        )

    def create(self, session: VerifiedPeerSession) -> LocalConnection:
        return LocalConnection(
            session=session,
            authority=self._authority,
            admission=self._admission,
            dispatcher=self._dispatcher,
            maximum_frame_bytes=self._settings.request_max_bytes,
            request_timeout_seconds=self._settings.request_timeout_seconds,
        )


class AtlasLocalAppServer:
    """Owns exactly one configured local transport and no HTTP routes."""

    def __init__(
        self,
        settings: HarnessSettings,
        authority_repository: AuthorityRepository,
        dispatcher: CommandDispatcher,
        *,
        signing_key: bytes | None = None,
    ) -> None:
        if not settings.enabled:
            raise ValueError("Atlas local app server requires enabled harness")
        if settings.loopback_http_enabled:
            raise ValueError("loopback HTTP is unavailable in this phase")
        self._settings = settings
        self._connection_factory = HarnessConnectionFactory(
            settings,
            authority_repository,
            dispatcher,
        )
        local_signing_key = signing_key
        if local_signing_key is None:
            local_signing_key = secrets.token_bytes(32)
        self._token_manager = PeerSessionTokenManager(local_signing_key)
        self._process_reader = UnixPeerCredentialReader()
        self._stream_runner = LocalStreamRunner(
            read_chunk_bytes=settings.request_max_bytes + 4,
            shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
        )
        self._unix_server: UnixHarnessServer | None = None
        self._stdio_task: asyncio.Task[object] | None = None

    async def start(self) -> None:
        if self._settings.local_transport != "unix":
            raise RuntimeError("configured stdio transport has no listener")
        if self._unix_server is not None:
            raise RuntimeError("Atlas local app server is already running")
        socket_path = self._settings.unix_socket_path
        if socket_path is None:
            raise RuntimeError("Unix socket path is not configured")
        server = UnixHarnessServer(
            socket_path=socket_path,
            handshake=UnixChallengeHandshake(
                self._process_reader,
                self._token_manager,
                timeout_seconds=self._settings.handshake_timeout_seconds,
            ),
            connection_factory=self._connection_factory,
            stream_runner=self._stream_runner,
            maximum_sessions=self._settings.active_sessions,
            stream_limit_bytes=self._settings.request_max_bytes + 4,
            shutdown_timeout_seconds=self._settings.shutdown_timeout_seconds,
        )
        await server.start()
        self._unix_server = server

    async def serve_stdio(
        self,
        reader: asyncio.StreamReader,
        writer: AsyncStreamWriter,
    ) -> None:
        if self._settings.local_transport != "stdio":
            raise RuntimeError("configured Unix transport cannot serve stdio")
        if self._stdio_task is not None:
            raise RuntimeError("stdio harness session is already active")
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("stdio harness requires an owned asyncio task")
        self._stdio_task = current_task
        stream_runner_started = False
        try:
            session = await StdioSessionAuthenticator(
                StdioParentCredentialReader(self._process_reader),
                self._token_manager,
            ).authenticate()
            connection = self._connection_factory.create(session)
            stream_runner_started = True
            await self._stream_runner.serve(reader, writer, connection)
        finally:
            if not stream_runner_started:
                writer.close()
                async with asyncio.timeout(
                    self._settings.shutdown_timeout_seconds
                ):
                    await writer.wait_closed()
            self._stdio_task = None

    async def close(self) -> None:
        unix_server = self._unix_server
        self._unix_server = None
        if unix_server is not None:
            await unix_server.close()
        stdio_task = self._stdio_task
        current_task = asyncio.current_task()
        if stdio_task is not None and stdio_task is not current_task:
            stdio_task.cancel()
            await asyncio.gather(stdio_task, return_exceptions=True)

    def snapshot(self) -> LocalAppServerSnapshot:
        unix_snapshot = None
        if self._unix_server is not None:
            unix_snapshot = self._unix_server.snapshot()
        return LocalAppServerSnapshot(
            transport=self._settings.local_transport,
            unix_listener_running=(
                unix_snapshot is not None and unix_snapshot.running
            ),
            stdio_session_active=self._stdio_task is not None,
            active_unix_sessions=(
                0 if unix_snapshot is None else unix_snapshot.active_sessions
            ),
        )
