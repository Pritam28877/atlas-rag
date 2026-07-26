"""Bounded one-time challenge handshake for authenticated Unix streams."""

from __future__ import annotations

import asyncio
import socket
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import StringConstraints, ValidationError

from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.runtime.framing import (
    HEADER_BYTES,
    FrameError,
    encode_frame,
)
from app.services.harness.runtime.peer_auth import (
    PeerAuthenticationError,
    PeerCredentials,
    PeerSessionTokenManager,
    VerifiedPeerSession,
)
from app.services.harness.runtime.streams import AsyncStreamWriter

MAXIMUM_HANDSHAKE_BYTES = 4_096
type SessionToken = Annotated[
    str,
    StringConstraints(min_length=32, max_length=2_048),
]


class UnixHandshakeErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    TIMEOUT = "timeout"


class UnixHandshakeError(PermissionError):
    """Client-safe handshake denial without token or peer disclosure."""

    def __init__(self, code: UnixHandshakeErrorCode) -> None:
        super().__init__("local connection authentication failed")
        self.code = code


class HandshakeChallenge(StrictProtocolModel):
    kind: Literal["peer_challenge"] = "peer_challenge"
    token: SessionToken


class HandshakeProof(StrictProtocolModel):
    kind: Literal["peer_proof"] = "peer_proof"
    token: SessionToken


class PeerCredentialSource(Protocol):
    async def read(self, peer_socket: socket.socket) -> PeerCredentials: ...


class UnixChallengeHandshake:
    """Authenticates one stream before any command bytes are accepted."""

    def __init__(
        self,
        credential_source: PeerCredentialSource,
        token_manager: PeerSessionTokenManager,
        *,
        timeout_seconds: float = 5,
    ) -> None:
        if not 0.001 <= timeout_seconds <= 30:
            raise ValueError(
                "handshake timeout must be between 1 ms and 30 seconds"
            )
        self._credential_source = credential_source
        self._token_manager = token_manager
        self._timeout_seconds = timeout_seconds

    async def authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: AsyncStreamWriter,
        peer_socket: socket.socket,
    ) -> VerifiedPeerSession:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                peer = await self._credential_source.read(peer_socket)
                token = self._token_manager.issue(peer)
                challenge = HandshakeChallenge(token=token)
                writer.write(
                    encode_frame(
                        challenge.model_dump_json().encode(),
                        maximum_frame_bytes=MAXIMUM_HANDSHAKE_BYTES,
                    )
                )
                await writer.drain()
                proof_payload = await self._read_frame(reader)
                proof = HandshakeProof.model_validate_json(proof_payload)
                return await asyncio.to_thread(
                    self._token_manager.verify_and_consume,
                    proof.token,
                    peer,
                )
        except TimeoutError as error:
            raise UnixHandshakeError(UnixHandshakeErrorCode.TIMEOUT) from error
        except (
            asyncio.IncompleteReadError,
            FrameError,
            PeerAuthenticationError,
            ValidationError,
            UnicodeError,
            ValueError,
            RuntimeError,
        ) as error:
            raise UnixHandshakeError(
                UnixHandshakeErrorCode.AUTHENTICATION_FAILED
            ) from error

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(HEADER_BYTES)
        payload_size = int.from_bytes(header, byteorder="big")
        if not 1 <= payload_size <= MAXIMUM_HANDSHAKE_BYTES:
            raise UnixHandshakeError(
                UnixHandshakeErrorCode.AUTHENTICATION_FAILED
            )
        return await reader.readexactly(payload_size)
