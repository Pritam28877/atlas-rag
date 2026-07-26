"""Secret-free references to Google Cloud credential sources."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import StringConstraints, model_validator

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    Sha256,
    StrictProtocolModel,
)

VertexIdentityReferenceId = Annotated[
    str,
    StringConstraints(pattern=r"^gcpid_[0-9a-f]{32}$"),
]
GcpProjectId = Annotated[
    str,
    StringConstraints(
        min_length=6,
        max_length=30,
        pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$",
    ),
]
ServiceAccountEmail = Annotated[
    str,
    StringConstraints(
        min_length=7,
        max_length=254,
        pattern=(
            r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]"
            r"@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
        ),
    ),
]


class VertexCredentialSourceKind(StrEnum):
    APPLICATION_DEFAULT = "application_default"
    EXTERNAL_ACCOUNT_FILE = "external_account_file"
    IMPERSONATED_SERVICE_ACCOUNT = "impersonated_service_account"


class VertexIdentityReference(StrictProtocolModel):
    identity_reference_id: VertexIdentityReferenceId
    credential_handle: ProviderCredentialHandle
    source: VertexCredentialSourceKind
    quota_project_id: GcpProjectId
    expected_principal_sha256: Sha256
    external_account_file: Path | None = None
    external_account_file_sha256: Sha256 | None = None
    target_service_account: ServiceAccountEmail | None = None

    @model_validator(mode="after")
    def validate_source_fields(self) -> Self:
        has_external_account = self.external_account_file is not None
        has_external_account_hash = (
            self.external_account_file_sha256 is not None
        )
        has_target_service_account = self.target_service_account is not None
        if self.source is VertexCredentialSourceKind.EXTERNAL_ACCOUNT_FILE:
            valid = (
                has_external_account
                and has_external_account_hash
                and not has_target_service_account
            )
        elif self.source is VertexCredentialSourceKind.IMPERSONATED_SERVICE_ACCOUNT:
            valid = (
                has_target_service_account
                and not has_external_account
                and not has_external_account_hash
            )
        else:
            valid = not (
                has_external_account
                or has_external_account_hash
                or has_target_service_account
            )
        if (
            valid
            and self.external_account_file is not None
            and not self.external_account_file.is_absolute()
        ):
            valid = False
        if not valid:
            raise ValueError("Vertex credential source fields are inconsistent")
        return self
