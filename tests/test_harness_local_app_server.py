import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.cli.harness import AtlasLocalAppServer
from app.core.harness_config import HarnessSettings
from app.services.harness.protocol import (
    CommandEnvelope,
    GrantRecord,
    GrantState,
    InlinePayload,
    WorkspaceRecord,
)
from app.services.harness.runtime import (
    AuthenticatedCommandContext,
    AuthoritySnapshot,
    CommandReply,
    FrameDecoder,
    HandshakeChallenge,
    HandshakeProof,
    UnixPeerCredentialReader,
    encode_frame,
)

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ID = "wsp_0123456789abcdef0123456789abcdef"
POLICY_VERSION = f"pol_{'0' * 64}"


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def command() -> CommandEnvelope:
    path = ROOT / "tests/fixtures/harness/protocol/reader-v1.0-command.json"
    return CommandEnvelope.model_validate_json(path.read_bytes())


def payload() -> InlinePayload:
    text = "workspace accepted"
    encoded = text.encode()
    return InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )


class Repository:
    def __init__(self, principal_id: str) -> None:
        now = datetime.now(UTC)
        workspace = WorkspaceRecord(
            workspace_id=WORKSPACE_ID,
            tenant_id=identifier("ten"),
            owner_principal_id=principal_id,
            root_uri="file:///srv/atlas/workspace",
            repository_fingerprint_sha256="1" * 64,
            policy_version=POLICY_VERSION,
            created_at=now - timedelta(days=1),
        )
        grant = GrantRecord(
            grant_id=identifier("grt"),
            principal_id=principal_id,
            workspace_id=WORKSPACE_ID,
            roles=("developer",),
            capabilities=("workspace.open",),
            policy_version=POLICY_VERSION,
            state=GrantState.ACTIVE,
            issued_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=5),
        )
        self.snapshot = AuthoritySnapshot(workspace=workspace, grants=(grant,))

    async def load_authority(self, workspace_id: str) -> AuthoritySnapshot:
        return self.snapshot


class Dispatcher:
    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> InlinePayload:
        return payload()


class Writer:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def settings(
    tmp_path: Path,
    transport: str,
) -> HarnessSettings:
    workspace_root = tmp_path / "workspaces"
    state_directory = tmp_path / "state"
    workspace_root.mkdir()
    state_directory.mkdir()
    values: dict[str, object] = {
        "enabled": True,
        "workspace_root": workspace_root,
        "state_directory": state_directory,
        "isolation_executable": Path("/usr/bin/true"),
        "local_transport": transport,
        "request_max_bytes": 64 * 1024,
        "request_timeout_seconds": 1,
    }
    if transport == "unix":
        run_directory = state_directory / "run"
        run_directory.mkdir(mode=0o700)
        values["unix_socket_path"] = run_directory / "atlas.sock"
    return HarnessSettings.model_validate(values)


async def principal_id_for(process_id: int) -> str:
    credentials = await UnixPeerCredentialReader().read_process_owner(process_id)
    return f"prn_{credentials.binding_sha256()[:32]}"


async def read_framed(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    return await reader.readexactly(int.from_bytes(header, "big"))


def test_unix_app_server_executes_real_authenticated_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness_settings = settings(tmp_path, "unix")
        repository = Repository(await principal_id_for(os.getpid()))
        app_server = AtlasLocalAppServer(
            harness_settings,
            repository,
            Dispatcher(),
            signing_key=b"k" * 32,
        )
        await app_server.start()
        assert app_server.snapshot().unix_listener_running
        assert harness_settings.unix_socket_path is not None

        reader, writer = await asyncio.open_unix_connection(
            harness_settings.unix_socket_path
        )
        challenge = HandshakeChallenge.model_validate_json(
            await read_framed(reader)
        )
        proof = HandshakeProof(token=challenge.token)
        writer.write(
            encode_frame(
                proof.model_dump_json().encode(),
                maximum_frame_bytes=4096,
            )
        )
        writer.write(
            encode_frame(
                command().model_dump_json().encode(),
                maximum_frame_bytes=harness_settings.request_max_bytes,
            )
        )
        await writer.drain()

        reply = CommandReply.model_validate_json(await read_framed(reader))
        assert reply.request_id == command().request_id
        assert reply.payload == payload()

        writer.close()
        await writer.wait_closed()
        await app_server.close()
        assert not app_server.snapshot().unix_listener_running

    asyncio.run(scenario())


def test_stdio_app_server_uses_same_authorized_command_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness_settings = settings(tmp_path, "stdio")
        repository = Repository(await principal_id_for(os.getppid()))
        app_server = AtlasLocalAppServer(
            harness_settings,
            repository,
            Dispatcher(),
            signing_key=b"k" * 32,
        )
        reader = asyncio.StreamReader()
        reader.feed_data(
            encode_frame(
                command().model_dump_json().encode(),
                maximum_frame_bytes=harness_settings.request_max_bytes,
            )
        )
        reader.feed_eof()
        writer = Writer()

        await app_server.serve_stdio(reader, writer)

        decoder = FrameDecoder(
            maximum_frame_bytes=harness_settings.request_max_bytes
        )
        reply = CommandReply.model_validate_json(decoder.feed(writer.frames[0])[0])
        assert reply.payload == payload()
        assert writer.closed
        assert not app_server.snapshot().stdio_session_active

    asyncio.run(scenario())


def test_disabled_and_wrong_transport_startup_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires enabled"):
        AtlasLocalAppServer(
            HarnessSettings(),
            Repository(identifier("prn")),
            Dispatcher(),
        )

    async def scenario() -> None:
        app_server = AtlasLocalAppServer(
            settings(tmp_path, "stdio"),
            Repository(await principal_id_for(os.getppid())),
            Dispatcher(),
        )
        with pytest.raises(RuntimeError, match="has no listener"):
            await app_server.start()

    asyncio.run(scenario())


def test_stdio_shutdown_cancels_and_closes_active_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        app_server = AtlasLocalAppServer(
            settings(tmp_path, "stdio"),
            Repository(await principal_id_for(os.getppid())),
            Dispatcher(),
        )
        reader = asyncio.StreamReader()
        writer = Writer()
        serve_task = asyncio.create_task(
            app_server.serve_stdio(reader, writer)
        )
        while not app_server.snapshot().stdio_session_active:
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

        await app_server.close()

        assert serve_task.done()
        assert writer.closed
        assert not app_server.snapshot().stdio_session_active

    asyncio.run(scenario())


def test_explicit_invalid_signing_key_never_uses_hidden_fallback(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        AtlasLocalAppServer(
            settings(tmp_path, "stdio"),
            Repository(identifier("prn")),
            Dispatcher(),
            signing_key=b"",
        )
