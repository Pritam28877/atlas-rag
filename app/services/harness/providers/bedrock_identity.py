"""Explicit secret-free AWS credential-source references."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    StrictProtocolModel,
)

BedrockIdentityReferenceId = Annotated[
    str,
    StringConstraints(pattern=r"^awsid_[0-9a-f]{32}$"),
]
AwsProfileName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    ),
]
AwsRoleArn = Annotated[
    str,
    StringConstraints(
        min_length=20,
        max_length=2_048,
        pattern=r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$",
    ),
]


class BedrockCredentialSourceKind(StrEnum):
    ENVIRONMENT = "environment"
    INSTANCE_METADATA = "instance_metadata"
    PROFILE = "profile"
    WEB_IDENTITY = "web_identity"


class BedrockIdentityReference(StrictProtocolModel):
    identity_reference_id: BedrockIdentityReferenceId
    credential_handle: ProviderCredentialHandle
    source: BedrockCredentialSourceKind
    profile_name: AwsProfileName | None = None
    role_arn: AwsRoleArn | None = None
    web_identity_token_file: Path | None = None
    role_session_name: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        pattern=r"^[A-Za-z0-9+=,.@_-]+$",
    )

    @model_validator(mode="after")
    def validate_source_fields(self) -> Self:
        has_profile = self.profile_name is not None
        has_role = self.role_arn is not None
        has_token = self.web_identity_token_file is not None
        has_session = self.role_session_name is not None
        if self.source is BedrockCredentialSourceKind.PROFILE:
            valid = has_profile and not (has_role or has_token or has_session)
        elif self.source is BedrockCredentialSourceKind.WEB_IDENTITY:
            valid = not has_profile and has_role and has_token and has_session
            token_file = self.web_identity_token_file
            if valid and token_file is not None and not token_file.is_absolute():
                valid = False
        else:
            valid = not (has_profile or has_role or has_token or has_session)
        if not valid:
            raise ValueError("AWS credential source fields are inconsistent")
        return self
