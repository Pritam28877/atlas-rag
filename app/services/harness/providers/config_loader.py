"""Bounded asynchronous loading for secret-free provider metadata."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from app.services.harness.providers.config_contracts import (
    LoadedProviderConfiguration,
    ProviderConfiguration,
)

MAXIMUM_PROVIDER_CONFIG_BYTES = 4 * 1024 * 1024
MAXIMUM_VALIDATION_DETAILS = 64


class ProviderConfigLoadErrorCode(StrEnum):
    INVALID_PATH = "invalid_path"
    NOT_REGULAR = "not_regular"
    SIZE = "size"
    READ = "read"
    VALIDATION = "validation"


class ProviderConfigLoadError(RuntimeError):
    def __init__(
        self,
        code: ProviderConfigLoadErrorCode,
        *,
        details: tuple[str, ...] = (),
    ) -> None:
        super().__init__("provider configuration could not be loaded")
        self.code = code
        self.details = details


async def load_provider_configuration(
    path: Path,
) -> LoadedProviderConfiguration:
    if not path.is_absolute():
        raise ProviderConfigLoadError(
            ProviderConfigLoadErrorCode.INVALID_PATH
        )
    raw_content = await asyncio.to_thread(_read_regular_file, path)
    try:
        configuration = ProviderConfiguration.model_validate_json(
            raw_content
        )
    except ValidationError as error:
        raise ProviderConfigLoadError(
            ProviderConfigLoadErrorCode.VALIDATION,
            details=_validation_details(error),
        ) from error
    return LoadedProviderConfiguration(
        content_sha256=hashlib.sha256(raw_content).hexdigest(),
        configuration=configuration,
    )


def _read_regular_file(path: Path) -> bytes:
    try:
        path_status = os.lstat(path)
    except OSError as error:
        raise ProviderConfigLoadError(
            ProviderConfigLoadErrorCode.READ
        ) from error
    if not stat.S_ISREG(path_status.st_mode):
        raise ProviderConfigLoadError(
            ProviderConfigLoadErrorCode.NOT_REGULAR
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path, flags)
    except OSError as error:
        raise ProviderConfigLoadError(
            ProviderConfigLoadErrorCode.READ
        ) from error
    try:
        file_status = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise ProviderConfigLoadError(
                ProviderConfigLoadErrorCode.NOT_REGULAR
            )
        if (
            file_status.st_dev != path_status.st_dev
            or file_status.st_ino != path_status.st_ino
        ):
            raise ProviderConfigLoadError(
                ProviderConfigLoadErrorCode.READ
            )
        if not 1 <= file_status.st_size <= MAXIMUM_PROVIDER_CONFIG_BYTES:
            raise ProviderConfigLoadError(
                ProviderConfigLoadErrorCode.SIZE
            )
        content = bytearray()
        while True:
            remaining = MAXIMUM_PROVIDER_CONFIG_BYTES + 1 - len(content)
            chunk = os.read(file_descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAXIMUM_PROVIDER_CONFIG_BYTES:
                raise ProviderConfigLoadError(
                    ProviderConfigLoadErrorCode.SIZE
                )
        if not content:
            raise ProviderConfigLoadError(
                ProviderConfigLoadErrorCode.SIZE
            )
        return bytes(content)
    except OSError as error:
        raise ProviderConfigLoadError(
            ProviderConfigLoadErrorCode.READ
        ) from error
    finally:
        os.close(file_descriptor)


def _validation_details(error: ValidationError) -> tuple[str, ...]:
    details: list[str] = []
    errors = error.errors(
        include_url=False,
        include_input=False,
    )
    for validation_error in errors[:MAXIMUM_VALIDATION_DETAILS]:
        location = ".".join(
            str(part) for part in validation_error.get("loc", ())
        )
        message = str(validation_error.get("msg", "invalid value"))
        detail = f"{location}: {message}" if location else message
        details.append(detail[:512])
    return tuple(details)
