import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.protocol import (
    AuthenticationMethod,
    CommandEnvelope,
    GrantRecord,
    GrantState,
    InlinePayload,
    PrincipalRecord,
    WorkspaceRecord,
)
from app.services.harness.runtime import (
    AuthenticatedCommandContext,
    AuthoritySnapshot,
    CommandAuthorityBinder,
    CommandErrorReply,
    CommandReply,
    ConnectionClosedError,
    ConnectionErrorCode,
    ConnectionFatalError,
    FrameDecoder,
    FrameError,
    FrameErrorCode,
    LocalConnection,
    RequestAdmission,
    VerifiedPeerSession,
    encode_frame,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 26, 16, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_0123456789abcdef0123456789abcdef"
POLICY_VERSION = f"pol_{'0' * 64}"


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def command() -> CommandEnvelope:
    path = ROOT / "tests/fixtures/harness/protocol/reader-v1.0-command.json"
    return CommandEnvelope.model_validate_json(path.read_bytes())


def inline_payload(text: str = "accepted") -> InlinePayload:
    encoded = text.encode()
    return InlinePayload(
        text=text,
        size_bytes=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def principal() -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=identifier("prn"),
        authentication_method=AuthenticationMethod.PEER_CREDENTIALS,
        issuer="atlas-local-peer",
        subject_sha256="0" * 64,
        session_binding_sha256="1" * 64,
        authenticated_at=NOW - timedelta(seconds=1),
    )


def verified_session() -> VerifiedPeerSession:
    return VerifiedPeerSession(
        principal=principal(),
        token_id_sha256="2" * 64,
        expires_at=NOW + timedelta(minutes=1),
    )


def authority_snapshot(
    *,
    workspace_id: str = WORKSPACE_ID,
) -> AuthoritySnapshot:
    workspace = WorkspaceRecord(
        workspace_id=workspace_id,
        tenant_id=identifier("ten"),
        owner_principal_id=principal().principal_id,
        root_uri="file:///srv/workspaces/atlas",
        repository_fingerprint_sha256="3" * 64,
        policy_version=POLICY_VERSION,
        created_at=NOW - timedelta(days=1),
    )
    grant = GrantRecord(
        grant_id=identifier("grt"),
        principal_id=principal().principal_id,
        workspace_id=workspace_id,
        roles=("developer",),
        capabilities=("workspace.open",),
        policy_version=POLICY_VERSION,
        state=GrantState.ACTIVE,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
    )
    return AuthoritySnapshot(workspace=workspace, grants=(grant,))


class AuthorityRepository:
    def __init__(self, snapshot: AuthoritySnapshot) -> None:
        self.snapshot = snapshot

    async def load_authority(self, workspace_id: str) -> AuthoritySnapshot:
        return self.snapshot


class Dispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> InlinePayload:
        self.calls += 1
        return inline_payload()


class BlockingDispatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> InlinePayload:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaned.set()


class FailingDispatcher:
    async def dispatch(
        self,
        context: AuthenticatedCommandContext,
        cancellation_event: asyncio.Event,
    ) -> InlinePayload:
        raise RuntimeError("sensitive backend detail")


class CapturingSender:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def __call__(self, frame: bytes) -> None:
        self.frames.append(frame)


def connection(
    dispatcher: object,
    *,
    snapshot: AuthoritySnapshot | None = None,
    timeout_seconds: float = 1,
) -> LocalConnection:
    repository = AuthorityRepository(snapshot or authority_snapshot())
    return LocalConnection(
        session=verified_session(),
        authority=CommandAuthorityBinder(repository, clock=lambda: NOW),
        admission=RequestAdmission(
            maximum_concurrency=1,
            maximum_request_seconds=2,
        ),
        dispatcher=dispatcher,  # type: ignore[arg-type]
        maximum_frame_bytes=64 * 1024,
        request_timeout_seconds=timeout_seconds,
    )


def request_frame() -> bytes:
    return encode_frame(
        command().model_dump_json().encode(),
        maximum_frame_bytes=64 * 1024,
    )


def decode_response(frame: bytes) -> bytes:
    decoder = FrameDecoder(maximum_frame_bytes=64 * 1024)
    return decoder.feed(frame)[0]


def test_valid_command_is_authorized_and_returns_typed_reply() -> None:
    async def scenario() -> None:
        dispatcher = Dispatcher()
        stream = connection(dispatcher)
        sender = CapturingSender()

        await stream.receive(request_frame(), sender)

        reply = CommandReply.model_validate_json(decode_response(sender.frames[0]))
        assert reply.request_id == command().request_id
        assert reply.response_kind == "workspace_status"
        assert reply.payload == inline_payload()
        assert dispatcher.calls == 1

    asyncio.run(scenario())


def test_malformed_command_and_workspace_substitution_are_generic() -> None:
    async def scenario() -> None:
        sender = CapturingSender()
        stream = connection(Dispatcher())
        malformed = encode_frame(
            b'{"secret":"must-not-echo"}',
            maximum_frame_bytes=64 * 1024,
        )
        await stream.receive(malformed, sender)
        malformed_reply = CommandErrorReply.model_validate_json(
            decode_response(sender.frames.pop())
        )
        assert malformed_reply.request_id is None
        assert (
            malformed_reply.error_code
            is ConnectionErrorCode.MALFORMED_COMMAND
        )

        substituted = connection(
            Dispatcher(),
            snapshot=authority_snapshot(workspace_id=identifier("wsp", 1)),
        )
        await substituted.receive(
            request_frame(),
            sender,
        )
        authority_reply = CommandErrorReply.model_validate_json(
            decode_response(sender.frames.pop())
        )
        assert authority_reply.request_id == command().request_id
        assert (
            authority_reply.error_code
            is ConnectionErrorCode.AUTHORITY_DENIED
        )

    asyncio.run(scenario())


def test_slow_dispatch_is_cancelled_and_awaited_at_deadline() -> None:
    async def scenario() -> None:
        dispatcher = BlockingDispatcher()
        stream = connection(dispatcher, timeout_seconds=0.01)
        sender = CapturingSender()

        await stream.receive(request_frame(), sender)

        reply = CommandErrorReply.model_validate_json(
            decode_response(sender.frames[0])
        )
        assert reply.error_code is ConnectionErrorCode.DEADLINE_EXCEEDED
        assert dispatcher.cleaned.is_set()

    asyncio.run(scenario())


def test_disconnect_cancels_owned_dispatch_before_close_returns() -> None:
    async def scenario() -> None:
        dispatcher = BlockingDispatcher()
        stream = connection(dispatcher)
        sender = CapturingSender()
        receive_task = asyncio.create_task(
            stream.receive(request_frame(), sender)
        )
        await dispatcher.started.wait()

        close_task = asyncio.create_task(stream.close())
        with pytest.raises(ConnectionClosedError):
            await receive_task
        await close_task

        assert dispatcher.cleaned.is_set()
        assert sender.frames == []
        with pytest.raises(ConnectionClosedError):
            await stream.receive(request_frame(), sender)

    asyncio.run(scenario())


def test_outer_transport_cancellation_cleans_owned_dispatch() -> None:
    async def scenario() -> None:
        dispatcher = BlockingDispatcher()
        stream = connection(dispatcher)
        sender = CapturingSender()
        receive_task = asyncio.create_task(
            stream.receive(request_frame(), sender)
        )
        await dispatcher.started.wait()

        receive_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receive_task

        assert dispatcher.cleaned.is_set()
        with pytest.raises(ConnectionClosedError):
            await stream.receive(request_frame(), sender)

    asyncio.run(scenario())


def test_sender_backpressure_prevents_unbounded_request_fanout() -> None:
    async def scenario() -> None:
        dispatcher = Dispatcher()
        stream = connection(dispatcher)
        send_started = asyncio.Event()
        release_send = asyncio.Event()
        sent: list[bytes] = []

        async def slow_sender(frame: bytes) -> None:
            sent.append(frame)
            if len(sent) == 1:
                send_started.set()
                await release_send.wait()

        receive_task = asyncio.create_task(
            stream.receive(request_frame() + request_frame(), slow_sender)
        )
        await send_started.wait()
        assert dispatcher.calls == 1
        release_send.set()
        await receive_task
        assert dispatcher.calls == 2
        assert len(sent) == 2

    asyncio.run(scenario())


def test_unexpected_dispatch_failure_is_redacted_and_fatal() -> None:
    async def scenario() -> None:
        stream = connection(FailingDispatcher())
        sender = CapturingSender()

        with pytest.raises(ConnectionFatalError) as fatal:
            await stream.receive(request_frame(), sender)

        reply = CommandErrorReply.model_validate_json(
            decode_response(sender.frames[0])
        )
        assert reply.error_code is ConnectionErrorCode.INTERNAL_ERROR
        assert "sensitive" not in decode_response(sender.frames[0]).decode()
        assert isinstance(fatal.value.__cause__, RuntimeError)
        with pytest.raises(ConnectionClosedError):
            await stream.receive(request_frame(), sender)

    asyncio.run(scenario())


def test_oversized_frame_is_fatal_before_dispatch() -> None:
    async def scenario() -> None:
        dispatcher = Dispatcher()
        stream = connection(dispatcher)
        sender = CapturingSender()
        header = (64 * 1024 + 1).to_bytes(4, byteorder="big")

        with pytest.raises(FrameError) as oversized:
            await stream.receive(header, sender)

        assert oversized.value.code is FrameErrorCode.FRAME_TOO_LARGE
        assert dispatcher.calls == 0
        with pytest.raises(ConnectionClosedError):
            await stream.receive(request_frame(), sender)

    asyncio.run(scenario())
