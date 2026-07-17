"""Durable publication job and manifest record types."""

import re
from dataclasses import dataclass
from uuid import UUID

from app.services.ingestion.repository_support import ClaimedNativeJob

SEARCH_TARGET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class SearchTarget:
    name: str
    version: str

    def __post_init__(self) -> None:
        if not SEARCH_TARGET_NAME.fullmatch(self.name):
            raise ValueError("persisted search target name is invalid")
        if not self.version or len(self.version) > 128:
            raise ValueError("persisted search target version is invalid")


@dataclass(frozen=True, slots=True)
class SearchTargetPublication:
    target: SearchTarget
    record_count: int
    publication_job_id: UUID | None = None
    publication_attempt: int | None = None

    def __post_init__(self) -> None:
        if self.record_count < 1:
            raise ValueError("search target record count must be positive")
        if (self.publication_job_id is None) != (self.publication_attempt is None):
            raise ValueError("publication job and attempt must be paired")
        if self.publication_attempt is not None and self.publication_attempt < 1:
            raise ValueError("publication attempt must be positive")


@dataclass(frozen=True, slots=True)
class SearchCleanupClaim:
    id: UUID
    tenant_id: UUID
    collection_id: UUID
    document_version_id: UUID
    activation_version_id: UUID | None
    target: SearchTarget
    attempt_number: int
    publication_job_id: UUID | None = None
    publication_attempt: int | None = None


@dataclass(frozen=True, slots=True)
class PendingSearchActivation:
    tenant_id: UUID
    collection_id: UUID
    document_version_id: UUID
    target: SearchTarget
    record_count: int
    publication_job_id: UUID
    publication_attempt: int


@dataclass(frozen=True, slots=True)
class ClaimedPublicationJob(ClaimedNativeJob):
    stage: str
    source_document_version_id: UUID
    normalized_document_version_id: UUID
    normalized_artifact_id: UUID
    normalized_object_key: str
    normalized_checksum_sha256: str
    normalized_generator_version: str
    chunk_object_key: str | None
    chunk_checksum_sha256: str | None
    chunk_generator_version: str | None
    embedding_object_key: str | None
    embedding_checksum_sha256: str | None
    embedding_generator_version: str | None


@dataclass(frozen=True, slots=True)
class EmbeddingRecord:
    chunk_id: UUID
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    chunk_id: UUID
    publication_kind: str
    external_record_id: str
