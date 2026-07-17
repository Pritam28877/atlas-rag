from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.services.catalog.enums import VersionState


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    retention_days: int = Field(default=0, ge=0, le=36500)
    document_quota: int = Field(default=10_000, ge=1, le=1_000_000)
    storage_quota_bytes: int = Field(ge=1)


class CollectionResponse(BaseModel):
    id: UUID
    name: str
    retention_days: int
    upload_max_bytes: int
    document_quota: int
    storage_quota_bytes: int
    legal_hold: bool
    created_at: datetime


class CollectionPage(BaseModel):
    items: list[CollectionResponse]
    next_cursor: str | None = None


class DocumentRegistration(BaseModel):
    source_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=500)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    content_type: Literal["application/pdf"] = "application/pdf"
    pipeline_profile: str = Field(default="pdf-v1", min_length=1, max_length=128)


class UploadTarget(BaseModel):
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str]
    expires_in_seconds: int


class DocumentRegistrationResponse(BaseModel):
    document_id: UUID
    document_version_id: UUID
    state: VersionState
    upload: UploadTarget


class UploadCompletionResponse(BaseModel):
    document_version_id: UUID
    job_id: UUID | None
    state: VersionState
    reason_code: str | None = None


class DocumentVersionStatus(BaseModel):
    document_version_id: UUID
    document_id: UUID
    collection_id: UUID
    state: VersionState
    stage: str | None
    progress_completed: int
    progress_total: int
    terminal_reason_code: str | None
    retry_eligible: bool
    artifacts_available: bool
    citations_ready: bool
    lexical_index_ready: bool
    vector_index_ready: bool
    created_at: datetime
    updated_at: datetime


class LifecycleRequest(BaseModel):
    operation: Literal["retry", "reprocess", "cancel", "delete"]
    pipeline_profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )

    @model_validator(mode="after")
    def validate_reprocess_profile(self) -> "LifecycleRequest":
        if self.operation == "reprocess" and self.pipeline_profile is None:
            raise ValueError("pipeline_profile is required for reprocess")
        if self.operation != "reprocess" and self.pipeline_profile is not None:
            raise ValueError("pipeline_profile is supported only for reprocess")
        return self


class LifecycleResponse(BaseModel):
    operation_id: UUID
    document_version_id: UUID
    operation: Literal["retry", "reprocess", "cancel", "delete"]
    status: Literal["pending", "running", "succeeded", "failed"]
    version_state: VersionState


class SafeError(BaseModel):
    detail: str
    code: str
