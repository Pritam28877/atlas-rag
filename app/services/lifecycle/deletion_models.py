"""Immutable records shared by deletion orchestration components."""

from dataclasses import dataclass
from uuid import UUID

from app.services.ingestion.publication_models import SearchTarget


@dataclass(frozen=True, slots=True)
class ClaimedDeletion:
    job_id: UUID
    tenant_id: UUID
    collection_id: UUID
    document_version_id: UUID
    request_id: UUID
    attempt_number: int
    physical_cleanup_due: bool
    object_keys: tuple[str, ...]
    search_targets: tuple[SearchTarget, ...] = ()
