"""Fail-closed provider destination, redirect, data, and address policy."""

from __future__ import annotations

import hashlib
import ipaddress
from enum import StrEnum
from typing import Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    DataClassification,
    ProviderName,
    Sha256,
    StrictProtocolModel,
)

MAXIMUM_EGRESS_URL_LENGTH = 2_048
MAXIMUM_EGRESS_ADDRESSES = 16
MAXIMUM_EGRESS_REDIRECTS = 5
MAXIMUM_EGRESS_BODY_BYTES = 16 * 1024 * 1024
SAFE_PATH_CHARACTERS = frozenset(
    "/:@-._~!$&'()*+,;="
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
)


class ProviderEgressPolicyErrorCode(StrEnum):
    ADDRESS = "address"
    CLASSIFICATION = "classification"
    DESTINATION = "destination"
    REDIRECT = "redirect"
    REQUEST_SIZE = "request_size"
    URL = "url"


class ProviderEgressPolicyError(RuntimeError):
    def __init__(self, code: ProviderEgressPolicyErrorCode) -> None:
        super().__init__("provider egress policy denied the request")
        self.code = code


class ProviderEgressPolicy(StrictProtocolModel):
    provider: ProviderName
    destination_url: str = Field(min_length=9, max_length=MAXIMUM_EGRESS_URL_LENGTH)
    destination_sha256: Sha256
    allowed_redirect_origins: tuple[str, ...] = Field(max_length=16)
    accepted_classifications: tuple[DataClassification, ...] = Field(
        min_length=1,
        max_length=4,
    )
    max_request_bytes: int = Field(ge=1, le=MAXIMUM_EGRESS_BODY_BYTES)
    max_response_bytes: int = Field(ge=1, le=MAXIMUM_EGRESS_BODY_BYTES)
    max_redirects: int = Field(
        default=0,
        ge=0,
        le=MAXIMUM_EGRESS_REDIRECTS,
    )

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        canonical_destination = canonical_provider_url(
            self.destination_url
        )
        if canonical_destination != self.destination_url:
            raise ValueError("provider destination URL must be canonical")
        expected_digest = provider_destination_sha256(
            canonical_destination
        )
        if self.destination_sha256 != expected_digest:
            raise ValueError("provider destination hash mismatch")
        canonical_origins = tuple(
            canonical_provider_origin(origin)
            for origin in self.allowed_redirect_origins
        )
        if canonical_origins != self.allowed_redirect_origins:
            raise ValueError("provider redirect origins must be canonical")
        if tuple(sorted(set(canonical_origins))) != canonical_origins:
            raise ValueError("provider redirect origins must be unique and sorted")
        classifications = self.accepted_classifications
        if tuple(sorted(set(classifications))) != classifications:
            raise ValueError(
                "provider egress classifications must be unique and sorted"
            )
        if self.max_redirects > 0 and not canonical_origins:
            raise ValueError("provider redirects require an allowed origin")
        return self


class AuthorizedEgressTarget(StrictProtocolModel):
    provider: ProviderName
    canonical_url: str
    destination_sha256: Sha256
    hostname: str
    port: int = Field(ge=1, le=65_535)
    resolved_addresses: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_EGRESS_ADDRESSES,
    )
    redirect_count: int = Field(
        ge=0,
        le=MAXIMUM_EGRESS_REDIRECTS,
    )


def authorize_egress_target(
    policy: ProviderEgressPolicy,
    *,
    target_url: str,
    classification: DataClassification,
    request_bytes: int,
    resolved_addresses: tuple[str, ...],
    redirect_count: int,
) -> AuthorizedEgressTarget:
    if classification not in policy.accepted_classifications:
        raise ProviderEgressPolicyError(
            ProviderEgressPolicyErrorCode.CLASSIFICATION
        )
    if not 1 <= request_bytes <= policy.max_request_bytes:
        raise ProviderEgressPolicyError(
            ProviderEgressPolicyErrorCode.REQUEST_SIZE
        )
    if not 0 <= redirect_count <= policy.max_redirects:
        raise ProviderEgressPolicyError(
            ProviderEgressPolicyErrorCode.REDIRECT
        )
    try:
        canonical_target = canonical_provider_url(target_url)
    except ValueError:
        raise ProviderEgressPolicyError(
            ProviderEgressPolicyErrorCode.URL
        ) from None
    target_parts = urlsplit(canonical_target)
    destination_parts = urlsplit(policy.destination_url)
    target_origin = _origin_from_canonical_url(canonical_target)
    destination_origin = _origin_from_canonical_url(
        policy.destination_url
    )
    if redirect_count == 0:
        if target_origin != destination_origin:
            raise ProviderEgressPolicyError(
                ProviderEgressPolicyErrorCode.DESTINATION
            )
        if not _path_is_within(
            target_parts.path,
            destination_parts.path,
        ):
            raise ProviderEgressPolicyError(
                ProviderEgressPolicyErrorCode.DESTINATION
            )
    elif target_origin not in {
        destination_origin,
        *policy.allowed_redirect_origins,
    }:
        raise ProviderEgressPolicyError(
            ProviderEgressPolicyErrorCode.REDIRECT
        )
    hostname = target_parts.hostname
    if hostname is None:
        raise ProviderEgressPolicyError(
            ProviderEgressPolicyErrorCode.URL
        )
    canonical_addresses = _public_addresses(resolved_addresses)
    return AuthorizedEgressTarget(
        provider=policy.provider,
        canonical_url=canonical_target,
        destination_sha256=policy.destination_sha256,
        hostname=hostname,
        port=target_parts.port or 443,
        resolved_addresses=canonical_addresses,
        redirect_count=redirect_count,
    )


def provider_destination_sha256(destination_url: str) -> str:
    canonical_url = canonical_provider_url(destination_url)
    return hashlib.sha256(canonical_url.encode()).hexdigest()


def canonical_provider_origin(value: str) -> str:
    canonical_url = canonical_provider_url(value)
    parts = urlsplit(canonical_url)
    if parts.path != "/":
        raise ValueError("provider origin cannot contain a path")
    return _origin_from_canonical_url(canonical_url)


def canonical_provider_url(value: str) -> str:
    if not 9 <= len(value) <= MAXIMUM_EGRESS_URL_LENGTH:
        raise ValueError("provider URL length is invalid")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError("provider URL contains unsafe characters")
    parts = urlsplit(value)
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise ValueError("provider URL must use HTTPS")
    if parts.username is not None or parts.password is not None:
        raise ValueError("provider URL cannot contain user information")
    if parts.query or parts.fragment:
        raise ValueError("provider URL cannot contain query or fragment")
    hostname = parts.hostname
    if hostname is None or "%" in hostname:
        raise ValueError("provider URL hostname is invalid")
    normalized_hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if (
        not normalized_hostname
        or _is_ip_literal(normalized_hostname)
        or not _valid_dns_hostname(normalized_hostname)
    ):
        raise ValueError("provider URL must use a DNS hostname")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("provider URL port is invalid") from error
    if port is not None and port < 1:
        raise ValueError("provider URL port is invalid")
    canonical_netloc = normalized_hostname
    if port not in {None, 443}:
        canonical_netloc = f"{normalized_hostname}:{port}"
    path = parts.path or "/"
    _validate_path(path)
    return urlunsplit(("https", canonical_netloc, path, "", ""))


def _validate_path(path: str) -> None:
    if "%" in path or "\\" in path or "//" in path:
        raise ValueError("provider URL path is ambiguous")
    if any(character not in SAFE_PATH_CHARACTERS for character in path):
        raise ValueError("provider URL path contains unsafe characters")
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("provider URL path contains dot segments")


def _origin_from_canonical_url(canonical_url: str) -> str:
    parts = urlsplit(canonical_url)
    return urlunsplit(("https", parts.netloc, "", "", ""))


def _path_is_within(target_path: str, configured_path: str) -> bool:
    if configured_path == "/":
        return True
    prefix = configured_path.rstrip("/")
    return target_path == configured_path or target_path.startswith(prefix + "/")


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _valid_dns_hostname(hostname: str) -> bool:
    if len(hostname) > 253:
        return False
    for label in hostname.split("."):
        if not 1 <= len(label) <= 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not all(
            character.isascii()
            and (character.isalnum() or character == "-")
            for character in label
        ):
            return False
    return True


def _public_addresses(addresses: tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(addresses) <= MAXIMUM_EGRESS_ADDRESSES:
        raise ProviderEgressPolicyError(
            ProviderEgressPolicyErrorCode.ADDRESS
        )
    canonical_addresses: list[str] = []
    for address in addresses:
        if "%" in address:
            raise ProviderEgressPolicyError(
                ProviderEgressPolicyErrorCode.ADDRESS
            )
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise ProviderEgressPolicyError(
                ProviderEgressPolicyErrorCode.ADDRESS
            ) from None
        if not parsed.is_global or parsed.is_multicast:
            raise ProviderEgressPolicyError(
                ProviderEgressPolicyErrorCode.ADDRESS
            )
        canonical_addresses.append(parsed.compressed)
    canonical = tuple(sorted(set(canonical_addresses)))
    if len(canonical) != len(addresses):
        raise ProviderEgressPolicyError(
            ProviderEgressPolicyErrorCode.ADDRESS
        )
    return canonical
