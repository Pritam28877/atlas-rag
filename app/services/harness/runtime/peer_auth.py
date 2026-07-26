"""One-time local session tokens bound to verified Unix peer credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Never, cast

from pydantic import Field

from app.services.harness.protocol import (
    AuthenticationMethod,
    PrincipalRecord,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)


class PeerAuthenticationErrorCode(StrEnum):
    INVALID_TOKEN = "invalid_token"
    EXPIRED_TOKEN = "expired_token"
    PEER_MISMATCH = "peer_mismatch"
    REPLAYED_TOKEN = "replayed_token"
    REPLAY_CAPACITY = "replay_capacity"


class PeerAuthenticationError(RuntimeError):
    def __init__(
        self,
        code: PeerAuthenticationErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class PeerCredentials(StrictProtocolModel):
    """Credentials read from the kernel plus immutable process evidence."""

    user_id: int = Field(ge=0, le=2**32 - 1)
    group_id: int = Field(ge=0, le=2**32 - 1)
    process_id: int = Field(ge=1, le=2**31 - 1)
    process_start_ticks: int = Field(ge=1)
    executable_sha256: Sha256

    def binding_sha256(self) -> Sha256:
        content = json.dumps(
            self.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(content).hexdigest()


class VerifiedPeerSession(StrictProtocolModel):
    principal: PrincipalRecord
    token_id_sha256: Sha256
    expires_at: UtcTimestamp


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode_base64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            f"{value}{padding}",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as error:
        raise PeerAuthenticationError(
            PeerAuthenticationErrorCode.INVALID_TOKEN,
            "local session token is invalid",
        ) from error


class PeerSessionTokenManager:
    """Issues and consumes bounded one-time tokens for a single server process."""

    def __init__(
        self,
        signing_key: bytes,
        *,
        token_ttl_seconds: int = 30,
        maximum_replay_entries: int = 4096,
        clock: Callable[[], float] = time.time,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("local session signing key must contain at least 32 bytes")
        if not 1 <= token_ttl_seconds <= 300:
            message = "local session token TTL must be between 1 and 300 seconds"
            raise ValueError(message)
        if not 1 <= maximum_replay_entries <= 65_536:
            raise ValueError("maximum replay entries must be between 1 and 65536")
        self._signing_key = bytes(signing_key)
        self._token_ttl_seconds = token_ttl_seconds
        self._maximum_replay_entries = maximum_replay_entries
        self._clock = clock
        self._entropy = entropy
        self._consumed: dict[str, int] = {}
        self._lock = threading.Lock()

    def issue(self, peer: PeerCredentials) -> str:
        issued_at_ms = int(self._clock() * 1000)
        expires_at_ms = issued_at_ms + self._token_ttl_seconds * 1000
        token_id_bytes = self._entropy(16)
        if len(token_id_bytes) != 16:
            raise RuntimeError("local session entropy source returned invalid length")
        payload = {
            "expires_at_ms": expires_at_ms,
            "issued_at_ms": issued_at_ms,
            "peer_sha256": peer.binding_sha256(),
            "token_id": _encode_base64(token_id_bytes),
            "version": 1,
        }
        payload_bytes = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        signature = hmac.digest(self._signing_key, payload_bytes, "sha256")
        return f"{_encode_base64(payload_bytes)}.{_encode_base64(signature)}"

    def verify_and_consume(
        self,
        token: str,
        peer: PeerCredentials,
    ) -> VerifiedPeerSession:
        payload_bytes, signature = self._decode_token(token)
        expected_signature = hmac.digest(
            self._signing_key,
            payload_bytes,
            "sha256",
        )
        if not hmac.compare_digest(signature, expected_signature):
            self._invalid()
        payload = self._payload(payload_bytes)
        issued_at_ms, expires_at_ms, token_id = self._validate_payload(payload)
        now_ms = int(self._clock() * 1000)
        if now_ms < issued_at_ms or now_ms >= expires_at_ms:
            raise PeerAuthenticationError(
                PeerAuthenticationErrorCode.EXPIRED_TOKEN,
                "local session token has expired",
            )
        if payload.get("peer_sha256") != peer.binding_sha256():
            raise PeerAuthenticationError(
                PeerAuthenticationErrorCode.PEER_MISMATCH,
                "local session token belongs to another peer",
            )
        self._consume_once(token_id, expires_at_ms, now_ms)
        return self._verified_session(
            token,
            token_id,
            peer,
            issued_at_ms,
            expires_at_ms,
        )

    def _decode_token(self, token: str) -> tuple[bytes, bytes]:
        if not isinstance(token, str) or not 32 <= len(token) <= 2048:
            self._invalid()
        parts = token.split(".")
        if len(parts) != 2:
            self._invalid()
        return _decode_base64(parts[0]), _decode_base64(parts[1])

    def _payload(self, payload_bytes: bytes) -> dict[str, object]:
        try:
            value = json.loads(payload_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PeerAuthenticationError(
                PeerAuthenticationErrorCode.INVALID_TOKEN,
                "local session token is invalid",
            ) from error
        if not isinstance(value, dict):
            self._invalid()
        return cast(dict[str, object], value)

    def _validate_payload(
        self,
        payload: dict[str, object],
    ) -> tuple[int, int, str]:
        issued_at_ms = payload.get("issued_at_ms")
        expires_at_ms = payload.get("expires_at_ms")
        token_id = payload.get("token_id")
        lifetime_ms = self._token_ttl_seconds * 1000
        valid = (
            payload.get("version") == 1
            and isinstance(issued_at_ms, int)
            and not isinstance(issued_at_ms, bool)
            and isinstance(expires_at_ms, int)
            and not isinstance(expires_at_ms, bool)
            and expires_at_ms - issued_at_ms == lifetime_ms
            and isinstance(token_id, str)
            and 16 <= len(token_id) <= 64
        )
        if not valid:
            self._invalid()
        return (
            cast(int, issued_at_ms),
            cast(int, expires_at_ms),
            cast(str, token_id),
        )

    def _consume_once(self, token_id: str, expires_at_ms: int, now_ms: int) -> None:
        token_id_sha256 = hashlib.sha256(token_id.encode()).hexdigest()
        with self._lock:
            expired_token_ids = [
                consumed_token_id
                for consumed_token_id, expiry in self._consumed.items()
                if expiry <= now_ms
            ]
            for consumed_token_id in expired_token_ids:
                del self._consumed[consumed_token_id]
            if token_id_sha256 in self._consumed:
                raise PeerAuthenticationError(
                    PeerAuthenticationErrorCode.REPLAYED_TOKEN,
                    "local session token was already consumed",
                )
            if len(self._consumed) >= self._maximum_replay_entries:
                raise PeerAuthenticationError(
                    PeerAuthenticationErrorCode.REPLAY_CAPACITY,
                    "local session replay ledger is full",
                )
            self._consumed[token_id_sha256] = expires_at_ms

    def _verified_session(
        self,
        token: str,
        token_id: str,
        peer: PeerCredentials,
        issued_at_ms: int,
        expires_at_ms: int,
    ) -> VerifiedPeerSession:
        subject_sha256 = peer.binding_sha256()
        principal = PrincipalRecord(
            principal_id=f"prn_{subject_sha256[:32]}",
            authentication_method=AuthenticationMethod.PEER_CREDENTIALS,
            issuer="atlas-local-peer",
            subject_sha256=subject_sha256,
            session_binding_sha256=hashlib.sha256(token.encode()).hexdigest(),
            authenticated_at=datetime.fromtimestamp(issued_at_ms / 1000, tz=UTC),
        )
        return VerifiedPeerSession(
            principal=principal,
            token_id_sha256=hashlib.sha256(token_id.encode()).hexdigest(),
            expires_at=datetime.fromtimestamp(expires_at_ms / 1000, tz=UTC),
        )

    def _invalid(self) -> Never:
        raise PeerAuthenticationError(
            PeerAuthenticationErrorCode.INVALID_TOKEN,
            "local session token is invalid",
        )
