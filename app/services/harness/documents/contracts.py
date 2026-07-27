"""Evidence-ledger records with immutable, tenant-scoped citations."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ArtifactId,
    BoundedLabel,
    BoundedReason,
    Sha256,
    StrictProtocolModel,
    TenantId,
    WorkspaceId,
)
from app.services.harness.protocol.conversation import SourceId

MAXIMUM_EVIDENCE_RECORDS = 4_096
MAXIMUM_QUOTE_BYTES = 128 * 1024


class EvidenceClassification(StrEnum):
    RELEVANT = "relevant"
    IRRELEVANT = "irrelevant"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class EvidenceFinishOutcome(StrEnum):
    GROUNDED = "grounded"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class DocumentVersion(StrictProtocolModel):
    document_id: SourceId
    tenant_id: TenantId
    workspace_id: WorkspaceId
    artifact_id: ArtifactId
    mime_type: str = Field(min_length=3, max_length=127)
    size_bytes: int = Field(ge=1, le=4 * 1024 * 1024 * 1024)
    content_sha256: Sha256
    revision_sha256: Sha256


class DocumentSpan(StrictProtocolModel):
    document_id: SourceId
    revision_sha256: Sha256
    page_number: int = Field(ge=1, le=1_000_000)
    start_offset: int = Field(ge=0, le=2_000_000_000)
    end_offset: int = Field(ge=0, le=2_000_000_000)
    text: str = Field(min_length=1, max_length=MAXIMUM_QUOTE_BYTES)
    text_sha256: Sha256

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.end_offset <= self.start_offset:
            raise ValueError("document span offsets must be increasing")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("document span text hash is invalid")
        return self


class EvidenceQuery(StrictProtocolModel):
    query_id: SourceId
    tenant_id: TenantId
    workspace_id: WorkspaceId
    text: str = Field(min_length=1, max_length=32 * 1024)
    retriever_revision: BoundedLabel
    tool_revision: BoundedLabel


class EvidenceCandidate(StrictProtocolModel):
    candidate_id: SourceId
    query_id: SourceId
    tenant_id: TenantId
    workspace_id: WorkspaceId
    span: DocumentSpan
    score_micros: int = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.span.document_id == "":
            raise ValueError("candidate span must identify a document")
        return self


class EvidenceClassificationRecord(StrictProtocolModel):
    candidate_id: SourceId
    classification: EvidenceClassification
    reason: BoundedReason


class EvidenceCitation(StrictProtocolModel):
    citation_id: SourceId
    candidate_id: SourceId
    query_id: SourceId
    tenant_id: TenantId
    workspace_id: WorkspaceId
    span: DocumentSpan
    quote: str = Field(min_length=1, max_length=MAXIMUM_QUOTE_BYTES)
    quote_sha256: Sha256
    retriever_revision: BoundedLabel
    tool_revision: BoundedLabel

    @model_validator(mode="after")
    def validate_exact_quote(self) -> Self:
        if self.quote != self.span.text:
            raise ValueError("citation quote does not match immutable span")
        if hashlib.sha256(self.quote.encode("utf-8")).hexdigest() != self.quote_sha256:
            raise ValueError("citation quote hash is invalid")
        return self


class EvidenceClaim(StrictProtocolModel):
    claim_id: SourceId
    text: str = Field(min_length=1, max_length=32 * 1024)
    citation_ids: tuple[SourceId, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_citations(self) -> Self:
        if tuple(sorted(set(self.citation_ids))) != self.citation_ids:
            raise ValueError("claim citations must be unique and sorted")
        return self


class EvidenceFinishRecord(StrictProtocolModel):
    outcome: EvidenceFinishOutcome
    claim_ids: tuple[SourceId, ...] = Field(max_length=256)
    reason: BoundedReason


class EvidenceLedgerSnapshot(StrictProtocolModel):
    sources: tuple[DocumentVersion, ...] = Field(max_length=MAXIMUM_EVIDENCE_RECORDS)
    queries: tuple[EvidenceQuery, ...] = Field(max_length=MAXIMUM_EVIDENCE_RECORDS)
    candidates: tuple[EvidenceCandidate, ...] = Field(
        max_length=MAXIMUM_EVIDENCE_RECORDS
    )
    classifications: tuple[EvidenceClassificationRecord, ...] = Field(
        max_length=MAXIMUM_EVIDENCE_RECORDS
    )
    citations: tuple[EvidenceCitation, ...] = Field(max_length=MAXIMUM_EVIDENCE_RECORDS)
    claims: tuple[EvidenceClaim, ...] = Field(max_length=MAXIMUM_EVIDENCE_RECORDS)
    finish: EvidenceFinishRecord | None = None


def record_digest(value: StrictProtocolModel) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
