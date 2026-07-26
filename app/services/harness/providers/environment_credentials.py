"""Explicit development-only environment credential backend."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    StrictProtocolModel,
)
from app.services.harness.providers.credential_material import (
    MAXIMUM_CREDENTIAL_BYTES,
    CredentialSecretMaterial,
    require_utc,
)

MAXIMUM_ENVIRONMENT_CREDENTIAL_REFERENCES = 256
EnvironmentVariableName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    ),
]


class EnvironmentCredentialErrorCode(StrEnum):
    DISABLED = "disabled"
    INVALID_REFERENCES = "invalid_references"
    MISSING = "missing"
    SIZE = "size"


class EnvironmentCredentialError(RuntimeError):
    def __init__(self, code: EnvironmentCredentialErrorCode) -> None:
        super().__init__("environment credential resolution failed")
        self.code = code


class EnvironmentCredentialReference(StrictProtocolModel):
    handle: ProviderCredentialHandle
    environment_variable: EnvironmentVariableName
    lease_ttl_seconds: int = Field(ge=1, le=3_600)


class EnvironmentCredentialBackend:
    """Reads only explicitly allowlisted variables in development mode."""

    def __init__(
        self,
        references: tuple[EnvironmentCredentialReference, ...],
        *,
        development_mode: bool,
        clock: Callable[[], datetime],
        environment: Mapping[bytes, bytes] | None = None,
    ) -> None:
        if not development_mode:
            raise EnvironmentCredentialError(
                EnvironmentCredentialErrorCode.DISABLED
            )
        if not 1 <= len(references) <= MAXIMUM_ENVIRONMENT_CREDENTIAL_REFERENCES:
            raise EnvironmentCredentialError(
                EnvironmentCredentialErrorCode.INVALID_REFERENCES
            )
        handles = tuple(reference.handle for reference in references)
        if tuple(sorted(set(handles))) != handles:
            raise EnvironmentCredentialError(
                EnvironmentCredentialErrorCode.INVALID_REFERENCES
            )
        variable_names = tuple(
            reference.environment_variable for reference in references
        )
        if len(set(variable_names)) != len(variable_names):
            raise EnvironmentCredentialError(
                EnvironmentCredentialErrorCode.INVALID_REFERENCES
            )
        self._references = {
            reference.handle: reference for reference in references
        }
        self._clock = clock
        self._environment = os.environb if environment is None else environment

    async def load(
        self,
        handle: ProviderCredentialHandle,
    ) -> CredentialSecretMaterial:
        reference = self._references.get(handle)
        if reference is None:
            raise EnvironmentCredentialError(
                EnvironmentCredentialErrorCode.MISSING
            )
        variable_name = reference.environment_variable.encode("ascii")
        secret = self._environment.get(variable_name)
        if secret is None:
            raise EnvironmentCredentialError(
                EnvironmentCredentialErrorCode.MISSING
            )
        if not 1 <= len(secret) <= MAXIMUM_CREDENTIAL_BYTES:
            raise EnvironmentCredentialError(
                EnvironmentCredentialErrorCode.SIZE
            )
        now = self._clock()
        require_utc(now, "environment credential clock")
        return CredentialSecretMaterial(
            bytearray(secret),
            expires_at=now
            + timedelta(seconds=reference.lease_ttl_seconds),
        )
