import asyncio
import socket

import pytest

from app.services.harness.runtime import (
    FrameDecoder,
    HandshakeChallenge,
    HandshakeProof,
    PeerCredentials,
    PeerSessionTokenManager,
    UnixChallengeHandshake,
    UnixHandshakeError,
    UnixHandshakeErrorCode,
    encode_frame,
)

MAXIMUM_HANDSHAKE_BYTES = 4_096
PEER = PeerCredentials(
    user_id=1000,
    group_id=1000,
    process_id=1234,
    process_start_ticks=5678,
    executable_sha256="0" * 64,
)


class CredentialSource:
    async def read(self, peer_socket: socket.socket) -> PeerCredentials:
        return PEER


class ProofWriter:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        *,
        proof_override: str | None = None,
    ) -> None:
        self._reader = reader
        self._proof_override = proof_override
        self._pending_frame: bytes | None = None
        self.closed = False

    def write(self, data: bytes) -> None:
        self._pending_frame = data

    async def drain(self) -> None:
        assert self._pending_frame is not None
        decoder = FrameDecoder(maximum_frame_bytes=MAXIMUM_HANDSHAKE_BYTES)
        challenge_payload = decoder.feed(self._pending_frame)[0]
        challenge = HandshakeChallenge.model_validate_json(challenge_payload)
        proof = HandshakeProof(
            token=self._proof_override or challenge.token,
        )
        self._reader.feed_data(
            encode_frame(
                proof.model_dump_json().encode(),
                maximum_frame_bytes=MAXIMUM_HANDSHAKE_BYTES,
            )
        )

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def token_manager() -> PeerSessionTokenManager:
    return PeerSessionTokenManager(
        b"k" * 32,
        entropy=lambda size: b"a" * size,
    )


def test_valid_challenge_proof_creates_peer_bound_session() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        writer = ProofWriter(reader)
        server_socket, client_socket = socket.socketpair()
        try:
            session = await UnixChallengeHandshake(
                CredentialSource(),
                token_manager(),
            ).authenticate(reader, writer, server_socket)
        finally:
            server_socket.close()
            client_socket.close()

        assert session.principal.subject_sha256 == PEER.binding_sha256()
        assert session.principal.session_binding_sha256 != PEER.binding_sha256()

    asyncio.run(scenario())


def test_replayed_proof_is_rejected_without_detail() -> None:
    async def scenario() -> None:
        manager = token_manager()
        first_reader = asyncio.StreamReader()
        first_writer = ProofWriter(first_reader)
        server_socket, client_socket = socket.socketpair()
        try:
            await UnixChallengeHandshake(
                CredentialSource(),
                manager,
            ).authenticate(first_reader, first_writer, server_socket)
            assert first_writer._pending_frame is not None
            decoder = FrameDecoder(maximum_frame_bytes=MAXIMUM_HANDSHAKE_BYTES)
            challenge = HandshakeChallenge.model_validate_json(
                decoder.feed(first_writer._pending_frame)[0]
            )

            replay_reader = asyncio.StreamReader()
            replay_writer = ProofWriter(
                replay_reader,
                proof_override=challenge.token,
            )
            with pytest.raises(UnixHandshakeError) as denied:
                await UnixChallengeHandshake(
                    CredentialSource(),
                    manager,
                ).authenticate(replay_reader, replay_writer, server_socket)
        finally:
            server_socket.close()
            client_socket.close()

        assert (
            denied.value.code
            is UnixHandshakeErrorCode.AUTHENTICATION_FAILED
        )
        assert str(denied.value) == "local connection authentication failed"

    asyncio.run(scenario())


def test_malformed_and_oversized_proofs_fail_closed() -> None:
    class RawProofWriter(ProofWriter):
        def __init__(self, reader: asyncio.StreamReader, frame: bytes) -> None:
            super().__init__(reader)
            self._raw_frame = frame

        async def drain(self) -> None:
            self._reader.feed_data(self._raw_frame)

    async def scenario(frame: bytes) -> None:
        reader = asyncio.StreamReader()
        writer = RawProofWriter(reader, frame)
        server_socket, client_socket = socket.socketpair()
        try:
            with pytest.raises(UnixHandshakeError) as denied:
                await UnixChallengeHandshake(
                    CredentialSource(),
                    token_manager(),
                ).authenticate(reader, writer, server_socket)
        finally:
            server_socket.close()
            client_socket.close()
        assert (
            denied.value.code
            is UnixHandshakeErrorCode.AUTHENTICATION_FAILED
        )

    asyncio.run(
        scenario(
            encode_frame(
                b'{"kind":"peer_proof","token":"not-valid"}',
                maximum_frame_bytes=MAXIMUM_HANDSHAKE_BYTES,
            )
        )
    )
    asyncio.run(scenario((MAXIMUM_HANDSHAKE_BYTES + 1).to_bytes(4, "big")))


def test_incomplete_proof_times_out_with_owned_deadline() -> None:
    class SilentWriter(ProofWriter):
        async def drain(self) -> None:
            return None

    async def scenario() -> None:
        reader = asyncio.StreamReader()
        writer = SilentWriter(reader)
        server_socket, client_socket = socket.socketpair()
        try:
            with pytest.raises(UnixHandshakeError) as denied:
                await UnixChallengeHandshake(
                    CredentialSource(),
                    token_manager(),
                    timeout_seconds=0.01,
                ).authenticate(reader, writer, server_socket)
        finally:
            server_socket.close()
            client_socket.close()
        assert denied.value.code is UnixHandshakeErrorCode.TIMEOUT

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout_seconds", (0, 31))
def test_handshake_timeout_is_bounded(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="between 1 ms and 30 seconds"):
        UnixChallengeHandshake(
            CredentialSource(),
            token_manager(),
            timeout_seconds=timeout_seconds,
        )
