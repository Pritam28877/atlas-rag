"""Exact-source Botocore loaders for static AWS credentials."""

from __future__ import annotations

import configparser
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Never, Protocol, cast

# Botocore does not publish PEP 561 typing metadata.
from botocore.credentials import (  # type: ignore[import-untyped]
    EnvProvider,
    SharedCredentialProvider,
)

from app.services.harness.providers.bedrock_credentials import (
    BedrockCredentialMaterial,
    BedrockCredentialResolutionError,
    BedrockCredentialResolutionErrorCode,
)
from app.services.harness.providers.bedrock_identity import (
    BedrockCredentialSourceKind,
    BedrockIdentityReference,
)

MAXIMUM_SHARED_CREDENTIAL_BYTES = 1024 * 1024
MAXIMUM_SHARED_CREDENTIAL_PROFILES = 256
_AWS_ENVIRONMENT_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
)


class _FrozenCredentials(Protocol):
    access_key: str
    secret_key: str
    token: str | None


class _BotocoreCredentials(Protocol):
    def get_frozen_credentials(self) -> _FrozenCredentials: ...


class BotocoreStaticCredentialBackend:
    """Loads one selected static source without invoking the default chain."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        source_environment = os.environ if environment is None else environment
        self._environment = {
            key: source_environment[key]
            for key in _AWS_ENVIRONMENT_KEYS
            if key in source_environment
        }

    def load(
        self,
        identity: BedrockIdentityReference,
    ) -> BedrockCredentialMaterial:
        if identity.source is BedrockCredentialSourceKind.ENVIRONMENT:
            credentials = cast(
                _BotocoreCredentials | None,
                EnvProvider(environ=self._environment).load(),
            )
        elif identity.source is BedrockCredentialSourceKind.PROFILE:
            credentials = self._load_profile(identity)
        else:
            self._reject()
        if credentials is None:
            self._reject()
        frozen = credentials.get_frozen_credentials()
        token = bytearray(frozen.token.encode()) if frozen.token is not None else None
        return BedrockCredentialMaterial(
            bytearray(frozen.access_key.encode()),
            bytearray(frozen.secret_key.encode()),
            token,
            expires_at=None,
        )

    def _load_profile(
        self,
        identity: BedrockIdentityReference,
    ) -> _BotocoreCredentials | None:
        credentials_file = identity.shared_credentials_file
        profile_name = identity.profile_name
        if credentials_file is None or profile_name is None:
            self._reject()
        profiles = _read_private_profiles(credentials_file)
        provider = SharedCredentialProvider(
            creds_filename=str(credentials_file),
            profile_name=profile_name,
            ini_parser=lambda _path: profiles,
        )
        return cast(_BotocoreCredentials | None, provider.load())

    @staticmethod
    def _reject() -> Never:
        raise BedrockCredentialResolutionError(
            BedrockCredentialResolutionErrorCode.LOAD
        )


def _read_private_profiles(
    path: Path,
) -> dict[str, dict[str, str]]:
    content = _read_private_file(path)
    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=True,
    )
    try:
        parser.read_string(content.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error):
        raise BedrockCredentialResolutionError(
            BedrockCredentialResolutionErrorCode.LOAD
        ) from None
    sections = parser.sections()
    if not 1 <= len(sections) <= MAXIMUM_SHARED_CREDENTIAL_PROFILES:
        raise BedrockCredentialResolutionError(
            BedrockCredentialResolutionErrorCode.LOAD
        )
    return {section: dict(parser.items(section, raw=True)) for section in sections}


def _read_private_file(path: Path) -> bytes:
    if not path.is_absolute():
        _reject_file()
    try:
        status = path.lstat()
    except OSError:
        _reject_file()
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) & 0o077
        or not 1 <= status.st_size <= MAXIMUM_SHARED_CREDENTIAL_BYTES
    ):
        _reject_file()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _reject_file()
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != status.st_dev or opened.st_ino != status.st_ino:
            _reject_file()
        content = bytearray()
        while True:
            remaining = MAXIMUM_SHARED_CREDENTIAL_BYTES + 1 - len(content)
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAXIMUM_SHARED_CREDENTIAL_BYTES:
                _reject_file()
        if not content:
            _reject_file()
        return bytes(content)
    finally:
        os.close(descriptor)


def _reject_file() -> Never:
    raise BedrockCredentialResolutionError(BedrockCredentialResolutionErrorCode.LOAD)
