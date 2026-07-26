"""Secret-free contracts around the privileged provider egress boundary."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    DataClassification,
    ProviderCredentialHandle,
    ProviderEgressAuditRecord,
    ProviderName,
    RequestId,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.base import BoundedReason
from app.services.harness.providers.credential_material import CredentialLease
from app.services.harness.providers.egress_policy import AuthorizedEgressTarget

MAXIMUM_SAFE_EGRESS_HEADERS = 32
SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)


class SafeEgressHeader(StrictProtocolModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9-]+$")
    value: str = Field(min_length=1, max_length=4_096)

    @model_validator(mode="after")
    def reject_sensitive_or_controlled_values(self) -> Self:
        if self.name in SENSITIVE_HEADER_NAMES:
            raise ValueError("credential-bearing header is gateway-owned")
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in self.value
        ):
            raise ValueError("egress header contains control characters")
        return self


class ProviderEgressRequestMetadata(StrictProtocolModel):
    request_id: RequestId
    provider: ProviderName
    credential_handle: ProviderCredentialHandle = Field(repr=False)
    target_url: str = Field(min_length=9, max_length=2_048, repr=False)
    classification: DataClassification
    content_type: str = Field(
        min_length=3,
        max_length=127,
        pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$",
    )
    safe_headers: tuple[SafeEgressHeader, ...] = Field(
        max_length=MAXIMUM_SAFE_EGRESS_HEADERS,
        repr=False,
    )
    body_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    body_sha256: Sha256

    @model_validator(mode="after")
    def validate_headers(self) -> Self:
        names = tuple(header.name for header in self.safe_headers)
        if tuple(sorted(set(names))) != names:
            raise ValueError("safe egress headers must be unique and sorted")
        return self


class ProviderEgressRequest:
    """Compiled body with a redacted representation and hash-bound metadata."""

    __slots__ = ("_body", "metadata")

    def __init__(
        self,
        *,
        request_id: RequestId,
        provider: ProviderName,
        credential_handle: ProviderCredentialHandle,
        target_url: str,
        classification: DataClassification,
        content_type: str,
        safe_headers: tuple[SafeEgressHeader, ...],
        body: bytes,
    ) -> None:
        if not isinstance(body, bytes) or not body:
            raise ValueError("provider egress body must be non-empty bytes")
        self._body = body
        self.metadata = ProviderEgressRequestMetadata(
            request_id=request_id,
            provider=provider,
            credential_handle=credential_handle,
            target_url=target_url,
            classification=classification,
            content_type=content_type,
            safe_headers=safe_headers,
            body_bytes=len(body),
            body_sha256=hashlib.sha256(body).hexdigest(),
        )

    def body(self) -> bytes:
        return self._body

    def __repr__(self) -> str:
        return (
            "ProviderEgressRequest("
            f"request_id={self.metadata.request_id!r}, "
            f"provider={self.metadata.provider!r}, "
            f"body_bytes={self.metadata.body_bytes}, "
            f"body_sha256={self.metadata.body_sha256!r}, "
            "target=<redacted>, headers=<redacted>, body=<redacted>)"
        )


def provider_egress_request_sha256(request: ProviderEgressRequest) -> str:
    metadata = request.metadata.model_dump_json().encode()
    return hashlib.sha256(metadata).hexdigest()


class ProviderEgressResponse:
    __slots__ = ("_body", "headers", "redirect_url", "status")

    def __init__(
        self,
        *,
        status: int,
        headers: tuple[SafeEgressHeader, ...],
        body: bytes,
        redirect_url: str | None = None,
    ) -> None:
        if not 100 <= status <= 599:
            raise ValueError("provider response status is invalid")
        if not isinstance(body, bytes):
            raise TypeError("provider response body must be bytes")
        self.status = status
        self.headers = headers
        self._body = body
        self.redirect_url = redirect_url

    def body(self) -> bytes:
        return self._body

    def __repr__(self) -> str:
        return (
            "ProviderEgressResponse("
            f"status={self.status}, headers={self.headers!r}, "
            f"redirect={'<present>' if self.redirect_url else None}, "
            "body=<redacted>)"
        )


class PayloadInspection(StrictProtocolModel):
    allowed: bool
    policy_revision_sha256: Sha256
    reason: BoundedReason


class ProviderPayloadInspector(Protocol):
    async def inspect(
        self,
        request: ProviderEgressRequest,
    ) -> PayloadInspection: ...


class ProviderAddressResolver(Protocol):
    async def resolve(
        self,
        hostname: str,
        port: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> tuple[str, ...]: ...


class ProviderEgressConnector(Protocol):
    async def send(
        self,
        request: ProviderEgressRequest,
        target: AuthorizedEgressTarget,
        credential: CredentialLease | None,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
        max_response_bytes: int,
    ) -> ProviderEgressResponse: ...


class ProviderEgressAuditSink(Protocol):
    async def record(self, record: ProviderEgressAuditRecord) -> None: ...
