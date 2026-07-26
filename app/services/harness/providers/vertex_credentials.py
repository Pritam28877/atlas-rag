"""Bound, refreshable Google credentials for regional Vertex egress."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import google.auth
import google.auth.impersonated_credentials
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

from app.services.harness.protocol import ProviderCredentialHandle
from app.services.harness.providers.credential_material import (
    CredentialBrokerError,
    CredentialBrokerErrorCode,
    CredentialSecretMaterial,
)
from app.services.harness.providers.vertex_identity import (
    VertexCredentialSourceKind,
    VertexIdentityReference,
)

VERTEX_CLOUD_PLATFORM_SCOPE = (
    "https://www.googleapis.com/auth/cloud-platform"
)
MAXIMUM_EXTERNAL_ACCOUNT_CONFIG_BYTES = 64 * 1024
MINIMUM_VERTEX_CREDENTIAL_TTL_SECONDS = 5


@dataclass(frozen=True, slots=True)
class RefreshedVertexCredential:
    access_token: str
    expires_at: datetime
    principal: str
    quota_project_id: str


class VertexCredentialLoader(Protocol):
    def __call__(
        self,
        identity: VertexIdentityReference,
    ) -> RefreshedVertexCredential: ...


class _ExternalCredentialLoader(Protocol):
    def __call__(
        self,
        info: dict[str, object],
        *,
        scopes: tuple[str, ...],
        quota_project_id: str,
        request: Request,
    ) -> tuple[Credentials, str | None]: ...


class _ImpersonatedCredentialFactory(Protocol):
    def __call__(
        self,
        *,
        source_credentials: Credentials,
        target_principal: str,
        target_scopes: tuple[str, ...],
        lifetime: int,
        quota_project_id: str,
    ) -> Credentials: ...


class _RefreshableCredential(Protocol):
    token: str | None
    expiry: datetime | None

    def refresh(self, request: Request) -> None: ...


class VertexCredentialBackend:
    def __init__(
        self,
        identities: tuple[VertexIdentityReference, ...],
        *,
        clock: Callable[[], datetime],
        loader: VertexCredentialLoader | None = None,
    ) -> None:
        by_handle = {
            identity.credential_handle: identity
            for identity in identities
        }
        if not identities or len(by_handle) != len(identities):
            raise ValueError("Vertex credential identities are invalid")
        self._identities = by_handle
        self._clock = clock
        self._loader = loader or _load_google_credential

    async def load(
        self,
        handle: ProviderCredentialHandle,
    ) -> CredentialSecretMaterial:
        identity = self._identities.get(handle)
        if identity is None:
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.UNKNOWN_HANDLE
            )
        try:
            refreshed = await asyncio.to_thread(self._loader, identity)
            return self._material(identity, refreshed)
        except CredentialBrokerError:
            raise
        except Exception:
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.BACKEND
            ) from None

    def _material(
        self,
        identity: VertexIdentityReference,
        refreshed: RefreshedVertexCredential,
    ) -> CredentialSecretMaterial:
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or refreshed.expires_at.tzinfo is None
            or refreshed.expires_at.utcoffset() != timedelta(0)
            or refreshed.expires_at
            <= now
            + timedelta(seconds=MINIMUM_VERTEX_CREDENTIAL_TTL_SECONDS)
            or refreshed.quota_project_id != identity.quota_project_id
            or hashlib.sha256(refreshed.principal.encode()).hexdigest()
            != identity.expected_principal_sha256
        ):
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.BACKEND
            )
        try:
            token = refreshed.access_token.encode("ascii")
        except UnicodeEncodeError:
            raise CredentialBrokerError(
                CredentialBrokerErrorCode.BACKEND
            ) from None
        return CredentialSecretMaterial(
            bytearray(token),
            expires_at=refreshed.expires_at,
        )


def _load_google_credential(
    identity: VertexIdentityReference,
) -> RefreshedVertexCredential:
    scopes = (VERTEX_CLOUD_PLATFORM_SCOPE,)
    request = Request()
    if identity.source is VertexCredentialSourceKind.EXTERNAL_ACCOUNT_FILE:
        external_loader = cast(
            _ExternalCredentialLoader,
            google.auth.load_credentials_from_dict,
        )
        credentials, _ = external_loader(
            _external_account_configuration(identity),
            scopes=scopes,
            quota_project_id=identity.quota_project_id,
            request=request,
        )
    else:
        credentials, _ = google.auth.default(
            scopes=scopes,
            quota_project_id=identity.quota_project_id,
            request=request,
        )
    if (
        identity.source
        is VertexCredentialSourceKind.IMPERSONATED_SERVICE_ACCOUNT
    ):
        target = identity.target_service_account
        if target is None:
            raise ValueError("Vertex impersonation target is missing")
        impersonated_factory = cast(
            _ImpersonatedCredentialFactory,
            google.auth.impersonated_credentials.Credentials,
        )
        credentials = impersonated_factory(
            source_credentials=credentials,
            target_principal=target,
            target_scopes=scopes,
            lifetime=900,
            quota_project_id=identity.quota_project_id,
        )
    refreshable = cast(_RefreshableCredential, credentials)
    refreshable.refresh(request)
    token = refreshable.token
    expiry = refreshable.expiry
    if not isinstance(token, str) or expiry is None:
        raise ValueError("Google credential refresh returned no token")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    quota_project = _quota_project(credentials)
    principal = _credential_principal(identity, credentials)
    return RefreshedVertexCredential(
        access_token=token,
        expires_at=expiry,
        principal=principal,
        quota_project_id=quota_project,
    )


def _external_account_configuration(
    identity: VertexIdentityReference,
) -> dict[str, object]:
    path = identity.external_account_file
    expected_sha256 = identity.external_account_file_sha256
    if path is None or expected_sha256 is None:
        raise ValueError("external account configuration is unbound")
    content = _read_bound_file(path)
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("external account configuration hash changed")
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or parsed.get("type") != "external_account":
        raise ValueError("external account configuration type is invalid")
    return cast(dict[str, object], parsed)


def _read_bound_file(path: Path) -> bytes:
    status = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) & 0o022
        or not 1 <= status.st_size <= MAXIMUM_EXTERNAL_ACCOUNT_CONFIG_BYTES
    ):
        raise ValueError("external account configuration file is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != status.st_dev
            or opened.st_ino != status.st_ino
        ):
            raise ValueError("external account configuration changed")
        content = os.read(
            descriptor,
            MAXIMUM_EXTERNAL_ACCOUNT_CONFIG_BYTES + 1,
        )
        if (
            len(content) != status.st_size
            or len(content) > MAXIMUM_EXTERNAL_ACCOUNT_CONFIG_BYTES
        ):
            raise ValueError("external account configuration size changed")
        return content
    finally:
        os.close(descriptor)


def _quota_project(credentials: object) -> str:
    value = getattr(credentials, "quota_project_id", None)
    if not isinstance(value, str):
        raise ValueError("Google credential has no quota project")
    return value


def _credential_principal(
    identity: VertexIdentityReference,
    credentials: object,
) -> str:
    if identity.target_service_account is not None:
        return identity.target_service_account
    for attribute in (
        "service_account_email",
        "signer_email",
        "account",
    ):
        value = getattr(credentials, attribute, None)
        if isinstance(value, str) and value:
            return value
    raise ValueError("Google credential principal is unavailable")
