"""Bounded document ingestion and retrieval interfaces for isolated adapters."""

from __future__ import annotations

import asyncio
import hashlib
from enum import StrEnum
from typing import Protocol

from pydantic import Field, model_validator

from app.services.harness.documents.contracts import (
    DocumentSpan,
    DocumentVersion,
    EvidenceCandidate,
    EvidenceQuery,
)
from app.services.harness.protocol.base import (
    ArtifactId,
    MediaType,
    Sha256,
    StrictProtocolModel,
    TenantId,
    WorkspaceId,
)
from app.services.harness.protocol.conversation import SourceId

MAXIMUM_DOCUMENT_BYTES = 256 * 1024 * 1024
MAXIMUM_DOCUMENT_PAGES = 10_000
MAXIMUM_DOCUMENT_CANDIDATES = 256
MAXIMUM_DOCUMENT_CHUNK_BYTES = 1024 * 1024


class DocumentIngestPolicy(StrictProtocolModel):
    allowed_mime_types: tuple[MediaType, ...] = Field(max_length=64)
    max_bytes: int = Field(default=32 * 1024 * 1024, ge=1, le=MAXIMUM_DOCUMENT_BYTES)
    max_pages: int = Field(default=1000, ge=1, le=MAXIMUM_DOCUMENT_PAGES)
    max_chunk_bytes: int = Field(
        default=64 * 1024,
        ge=1,
        le=MAXIMUM_DOCUMENT_CHUNK_BYTES,
    )

    @model_validator(mode="after")
    def validate_mimes(self) -> DocumentIngestPolicy:
        if tuple(sorted(set(self.allowed_mime_types))) != self.allowed_mime_types:
            raise ValueError("document MIME types must be unique and sorted")
        return self


class DocumentIngestRequest(StrictProtocolModel):
    document_id: SourceId
    tenant_id: TenantId
    workspace_id: WorkspaceId
    artifact_id: ArtifactId
    mime_type: MediaType
    expected_size_bytes: int = Field(ge=1, le=MAXIMUM_DOCUMENT_BYTES)
    expected_content_sha256: Sha256
    parser_revision: str = Field(min_length=1, max_length=128)


class DocumentBlobReader(Protocol):
    async def read(self, maximum_bytes: int) -> bytes: ...


class IsolatedDocumentParser(Protocol):
    async def parse(
        self,
        request: DocumentIngestRequest,
        content: bytes,
    ) -> tuple[DocumentSpan, ...]: ...

    async def close(self) -> None: ...


class IsolatedDocumentRetriever(Protocol):
    async def search(
        self,
        query: EvidenceQuery,
    ) -> tuple[EvidenceCandidate, ...]: ...


class DocumentAdapterErrorCode(StrEnum):
    SCOPE = "scope"
    SIZE = "size"
    DIGEST = "digest"
    MIME = "mime"
    PARSER = "parser"
    RETRIEVER = "retriever"
    CANDIDATE_SCOPE = "candidate_scope"


class DocumentAdapterError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("document adapter operation failed")
        self.code = code


class IsolatedDocumentAdapter:
    def __init__(
        self,
        policy: DocumentIngestPolicy,
        parser: IsolatedDocumentParser,
        retriever: IsolatedDocumentRetriever,
    ) -> None:
        self._policy = DocumentIngestPolicy.model_validate(policy.model_dump())
        self._parser = parser
        self._retriever = retriever

    async def ingest(
        self,
        request: DocumentIngestRequest,
        reader: DocumentBlobReader,
    ) -> tuple[DocumentVersion, tuple[DocumentSpan, ...]]:
        verified = DocumentIngestRequest.model_validate(request.model_dump())
        if verified.mime_type not in self._policy.allowed_mime_types:
            raise DocumentAdapterError(DocumentAdapterErrorCode.MIME)
        content = bytearray()
        try:
            while True:
                chunk = await reader.read(self._policy.max_chunk_bytes)
                if not isinstance(chunk, bytes) or (
                    len(chunk) > self._policy.max_chunk_bytes
                ):
                    raise DocumentAdapterError(DocumentAdapterErrorCode.SIZE)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > self._policy.max_bytes:
                    raise DocumentAdapterError(DocumentAdapterErrorCode.SIZE)
            if len(content) != verified.expected_size_bytes:
                raise DocumentAdapterError(DocumentAdapterErrorCode.SIZE)
            digest = hashlib.sha256(content).hexdigest()
            if digest != verified.expected_content_sha256:
                raise DocumentAdapterError(DocumentAdapterErrorCode.DIGEST)
            spans = await self._parser.parse(verified, bytes(content))
        except DocumentAdapterError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise DocumentAdapterError(DocumentAdapterErrorCode.PARSER) from None
        finally:
            content[:] = bytes(len(content))
        if len(spans) > self._policy.max_pages:
            raise DocumentAdapterError(DocumentAdapterErrorCode.PARSER)
        verified_spans = tuple(
            DocumentSpan.model_validate(span.model_dump()) for span in spans
        )
        revision = hashlib.sha256(
            f"{verified.artifact_id}:{verified.expected_content_sha256}:{verified.parser_revision}".encode()
        ).hexdigest()
        for span in verified_spans:
            if (
                span.document_id != verified.document_id
                or span.revision_sha256 != revision
            ):
                raise DocumentAdapterError(DocumentAdapterErrorCode.PARSER)
        return (
            DocumentVersion(
                document_id=verified.document_id,
                tenant_id=verified.tenant_id,
                workspace_id=verified.workspace_id,
                artifact_id=verified.artifact_id,
                mime_type=verified.mime_type,
                size_bytes=verified.expected_size_bytes,
                content_sha256=verified.expected_content_sha256,
                revision_sha256=revision,
            ),
            verified_spans,
        )

    async def search(self, query: EvidenceQuery) -> tuple[EvidenceCandidate, ...]:
        verified_query = EvidenceQuery.model_validate(query.model_dump())
        try:
            candidates = await self._retriever.search(verified_query)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise DocumentAdapterError(DocumentAdapterErrorCode.RETRIEVER) from None
        if len(candidates) > MAXIMUM_DOCUMENT_CANDIDATES:
            raise DocumentAdapterError(DocumentAdapterErrorCode.RETRIEVER)
        verified = tuple(
            EvidenceCandidate.model_validate(candidate.model_dump())
            for candidate in candidates
        )
        if any(
            candidate.tenant_id != verified_query.tenant_id
            or candidate.workspace_id != verified_query.workspace_id
            for candidate in verified
        ):
            raise DocumentAdapterError(DocumentAdapterErrorCode.CANDIDATE_SCOPE)
        return verified

    async def close(self) -> None:
        failures: list[BaseException] = []
        for component in (self._parser, self._retriever):
            close = getattr(component, "close", None)
            if close is None:
                continue
            try:
                await close()
            except BaseException as error:
                failures.append(error)
        if failures:
            raise DocumentAdapterError(DocumentAdapterErrorCode.PARSER)
