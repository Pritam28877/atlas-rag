"""SSRF-resistant endpoint and route policy for local-compatible servers."""

from __future__ import annotations

import hashlib
import ipaddress
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    Region,
    RouteId,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers.config_contracts import (
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
)
from app.services.harness.providers.local_compatible_identity import (
    LocalAuthenticationMode,
    LocalCompatibleIdentityReference,
    LocalIdentityReferenceId,
)

MAXIMUM_LOCAL_ENDPOINT_URL = 2_048
SAFE_LOCAL_PATH_CHARACTERS = frozenset(
    "/:@-._~!$&'()*+,;="
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
)


class LocalEndpointMode(StrEnum):
    LOOPBACK = "loopback"
    REMOTE_TLS = "remote_tls"


class LocalCompatibleRoutePolicy(StrictProtocolModel):
    route_id: RouteId
    model_id: ModelName
    region: Region
    credential_handle: ProviderCredentialHandle
    identity_reference_id: LocalIdentityReferenceId
    endpoint_mode: LocalEndpointMode
    endpoint_url: str = Field(min_length=9, max_length=MAXIMUM_LOCAL_ENDPOINT_URL)
    destination_sha256: Sha256
    allowed_remote_hostnames: tuple[str, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        endpoint = canonical_local_compatible_url(self.endpoint_url)
        if endpoint != self.endpoint_url:
            raise ValueError("local-compatible endpoint must be canonical")
        parts = urlsplit(endpoint)
        hostname = parts.hostname
        if hostname is None:
            raise ValueError("local-compatible endpoint hostname is missing")
        address = _ip_address(hostname)
        remote_hosts = self.allowed_remote_hostnames
        if tuple(sorted(set(remote_hosts))) != remote_hosts:
            raise ValueError("remote endpoint allowlist must be unique and sorted")
        for remote_host in remote_hosts:
            if _canonical_dns_hostname(remote_host) != remote_host:
                raise ValueError("remote endpoint allowlist must be canonical")
        if self.endpoint_mode is LocalEndpointMode.LOOPBACK:
            valid_mode = (
                address is not None
                and address.is_loopback
                and parts.port is not None
                and not remote_hosts
            )
        else:
            valid_mode = (
                address is None
                and parts.scheme == "https"
                and hostname in remote_hosts
            )
        if not valid_mode:
            raise ValueError("local-compatible endpoint mode is inconsistent")
        if local_destination_sha256(endpoint) != self.destination_sha256:
            raise ValueError("local-compatible endpoint hash does not match")
        return self

    def responses_url(self) -> str:
        return f"{self.endpoint_url}responses"


class AuthorizedLocalCompatibleRoute(StrictProtocolModel):
    provider: Literal["local-compatible"] = "local-compatible"
    route_id: RouteId
    model_id: ModelName
    region: Region
    credential_handle: ProviderCredentialHandle
    identity_reference_id: LocalIdentityReferenceId
    authentication: LocalAuthenticationMode
    endpoint_mode: LocalEndpointMode
    endpoint_url: str
    responses_url: str
    destination_sha256: Sha256


def authorize_local_compatible_route(
    loaded: LoadedProviderConfiguration,
    route: ProviderRouteConfiguration,
    policy: LocalCompatibleRoutePolicy,
    identity: LocalCompatibleIdentityReference,
) -> AuthorizedLocalCompatibleRoute:
    bindings = {
        binding.handle: binding for binding in loaded.configuration.credential_bindings
    }
    binding = bindings.get(route.credential_handle)
    if (
        not route.enabled
        or route.provider != "local-compatible"
        or route.route_id != policy.route_id
        or route.model != policy.model_id
        or route.region != policy.region
        or route.credential_handle != policy.credential_handle
        or identity.identity_reference_id != policy.identity_reference_id
        or identity.credential_handle != route.credential_handle
        or binding is None
        or binding.provider != "local-compatible"
        or binding.destination_sha256 != policy.destination_sha256
    ):
        raise ValueError("local-compatible route authorization failed")
    return AuthorizedLocalCompatibleRoute(
        route_id=route.route_id,
        model_id=route.model,
        region=route.region,
        credential_handle=route.credential_handle,
        identity_reference_id=identity.identity_reference_id,
        authentication=identity.authentication,
        endpoint_mode=policy.endpoint_mode,
        endpoint_url=policy.endpoint_url,
        responses_url=policy.responses_url(),
        destination_sha256=policy.destination_sha256,
    )


def local_destination_sha256(endpoint_url: str) -> str:
    canonical = canonical_local_compatible_url(endpoint_url)
    return hashlib.sha256(canonical.encode()).hexdigest()


def canonical_local_compatible_url(value: str) -> str:
    if not 9 <= len(value) <= MAXIMUM_LOCAL_ENDPOINT_URL:
        raise ValueError("local-compatible endpoint length is invalid")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError("local-compatible endpoint contains unsafe characters")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("local-compatible endpoint scheme is invalid")
    if parts.username is not None or parts.password is not None:
        raise ValueError("local-compatible endpoint cannot contain user information")
    if parts.query or parts.fragment:
        raise ValueError("local-compatible endpoint cannot contain query or fragment")
    hostname = parts.hostname
    if hostname is None or "%" in hostname:
        raise ValueError("local-compatible endpoint hostname is invalid")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("local-compatible endpoint port is invalid") from error
    address = _ip_address(hostname)
    if address is not None:
        if not address.is_loopback:
            raise ValueError("IP endpoints must use a loopback address")
        if port is None:
            raise ValueError("loopback endpoint requires an explicit port")
        canonical_hostname = address.compressed
        if address.version == 6:
            canonical_hostname = f"[{canonical_hostname}]"
    else:
        canonical_hostname = _canonical_dns_hostname(hostname)
        if parts.scheme != "https":
            raise ValueError("remote endpoints require TLS")
    canonical_port = ""
    if address is not None:
        canonical_port = f":{port}"
    elif port is not None and not (
        (parts.scheme == "https" and port == 443)
        or (parts.scheme == "http" and port == 80)
    ):
        canonical_port = f":{port}"
    path = parts.path or "/"
    _validate_path(path)
    if not path.endswith("/"):
        raise ValueError("local-compatible endpoint path must end with a slash")
    return urlunsplit(
        (
            parts.scheme,
            f"{canonical_hostname}{canonical_port}",
            path,
            "",
            "",
        )
    )


def _canonical_dns_hostname(hostname: str) -> str:
    normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if len(normalized) > 253:
        raise ValueError("remote endpoint hostname is invalid")
    for label in normalized.split("."):
        if (
            not 1 <= len(label) <= 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(
                character.isascii()
                and (character.isalnum() or character == "-")
                for character in label
            )
        ):
            raise ValueError("remote endpoint hostname is invalid")
    return normalized


def _validate_path(path: str) -> None:
    if (
        "%" in path
        or "\\" in path
        or "//" in path
        or any(character not in SAFE_LOCAL_PATH_CHARACTERS for character in path)
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        raise ValueError("local-compatible endpoint path is unsafe")


def _ip_address(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None
