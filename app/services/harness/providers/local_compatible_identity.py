"""Secret-free authentication references for local-compatible endpoints."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import StringConstraints, model_validator

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    StrictProtocolModel,
)
from app.services.harness.providers.environment_credentials import (
    EnvironmentVariableName,
)

LocalIdentityReferenceId = Annotated[
    str,
    StringConstraints(pattern=r"^locid_[0-9a-f]{32}$"),
]


class LocalAuthenticationMode(StrEnum):
    NONE = "none"
    BEARER_ENVIRONMENT = "bearer_environment"


class LocalCompatibleIdentityReference(StrictProtocolModel):
    identity_reference_id: LocalIdentityReferenceId
    credential_handle: ProviderCredentialHandle
    authentication: LocalAuthenticationMode
    bearer_environment_variable: EnvironmentVariableName | None = None

    @model_validator(mode="after")
    def validate_authentication_fields(self) -> Self:
        has_bearer_reference = self.bearer_environment_variable is not None
        if has_bearer_reference != (
            self.authentication is LocalAuthenticationMode.BEARER_ENVIRONMENT
        ):
            raise ValueError("local authentication fields are inconsistent")
        return self
