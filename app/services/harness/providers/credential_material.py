"""Redacted mutable credential material and lease contracts."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    ProviderName,
    Sha256,
)

MAXIMUM_CREDENTIAL_BYTES = 64 * 1024


class CredentialBrokerErrorCode(StrEnum):
    ACTIVE_LIMIT = "active_limit"
    BACKEND = "backend"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    DESTINATION_MISMATCH = "destination_mismatch"
    EXPIRED = "expired"
    PROVIDER_MISMATCH = "provider_mismatch"
    RELEASED = "released"
    UNKNOWN_HANDLE = "unknown_handle"


class CredentialBrokerError(RuntimeError):
    def __init__(self, code: CredentialBrokerErrorCode) -> None:
        super().__init__("credential broker operation rejected")
        self.code = code


class CredentialSecretMaterial:
    """Mutable backend result whose storage can be wiped after dispatch."""

    __slots__ = ("_secret", "expires_at")

    def __init__(self, secret: bytearray, *, expires_at: datetime) -> None:
        if not isinstance(secret, bytearray):
            raise TypeError("credential secret must use mutable byte storage")
        if not 1 <= len(secret) <= MAXIMUM_CREDENTIAL_BYTES:
            raise ValueError("credential secret size is outside the safe bound")
        require_utc(expires_at, "credential expiry")
        self._secret = secret
        self.expires_at = expires_at

    def claim(self) -> bytearray:
        if not self._secret:
            raise CredentialBrokerError(CredentialBrokerErrorCode.RELEASED)
        secret = self._secret
        self._secret = bytearray()
        return secret

    def zero(self) -> None:
        zero_buffer(self._secret)

    def __repr__(self) -> str:
        return "CredentialSecretMaterial(<redacted>)"


class CredentialSecretBackend(Protocol):
    async def load(
        self,
        handle: ProviderCredentialHandle,
    ) -> CredentialSecretMaterial: ...


class CredentialLease:
    """Non-serializable scoped view over temporary mutable secret bytes."""

    __slots__ = (
        "_broker_identity",
        "_lease_id",
        "_released",
        "_secret",
        "destination_sha256",
        "expires_at",
        "handle",
        "provider",
    )

    def __init__(
        self,
        *,
        broker_identity: object,
        lease_id: int,
        handle: ProviderCredentialHandle,
        provider: ProviderName,
        destination_sha256: Sha256,
        expires_at: datetime,
        secret: bytearray,
    ) -> None:
        self._broker_identity = broker_identity
        self._lease_id = lease_id
        self._released = False
        self._secret = secret
        self.handle = handle
        self.provider = provider
        self.destination_sha256 = destination_sha256
        self.expires_at = expires_at

    @property
    def released(self) -> bool:
        return self._released

    def secret_view(self) -> memoryview:
        if self._released:
            raise CredentialBrokerError(CredentialBrokerErrorCode.RELEASED)
        return memoryview(self._secret).toreadonly()

    def release(self) -> None:
        if self._released:
            return
        zero_buffer(self._secret)
        self._released = True

    def __repr__(self) -> str:
        return (
            "CredentialLease("
            f"handle={self.handle!r}, provider={self.provider!r}, "
            f"destination_sha256={self.destination_sha256!r}, "
            f"expires_at={self.expires_at!r}, secret=<redacted>)"
        )


def require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must use UTC")


def zero_buffer(secret: bytearray) -> None:
    secret[:] = bytes(len(secret))
