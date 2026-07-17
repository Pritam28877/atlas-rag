"""Tenant-scoped OpenSearch publication adapter with bounded bulk writes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.services.ingestion.chunking import ChunkRecord
from app.services.ingestion.embedding_artifacts import EmbeddedChunk
from app.services.ingestion.publication_models import PublicationRecord, SearchTarget
from app.services.ingestion.search_transport import (
    OpenSearchTransport,
    SearchPublicationError,
)


@dataclass(frozen=True, slots=True)
class SearchRecord:
    tenant_id: UUID
    collection_id: UUID
    document_version_id: UUID
    chunk: ChunkRecord
    embedding: EmbeddedChunk
    publication_job_id: UUID
    publication_attempt: int
    pipeline_profile: str = "unknown"
    embedding_provider: str = "unknown"
    embedding_model: str = "unknown"
    embedding_model_version: str = "unknown"


class SearchIndexAdapter(Protocol):
    target_name: str
    target_version: str

    async def publish(
        self, records: Sequence[SearchRecord]
    ) -> list[PublicationRecord]: ...

    async def activate_version(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        expected_records: int,
        target: SearchTarget | None = None,
        publication_job_id: UUID | None = None,
        publication_attempt: int | None = None,
    ) -> None: ...

    async def deactivate_version(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        expected_records: int,
        target: SearchTarget | None = None,
        publication_job_id: UUID | None = None,
        publication_attempt: int | None = None,
    ) -> None: ...

    async def delete_version(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        target: SearchTarget | None = None,
        publication_job_id: UUID | None = None,
        publication_attempt: int | None = None,
    ) -> int: ...

    async def close(self) -> None: ...


class OpenSearchIndexAdapter(OpenSearchTransport):

    async def publish(
        self, records: Sequence[SearchRecord]
    ) -> list[PublicationRecord]:
        if not records:
            raise ValueError("search publication batch cannot be empty")
        await self._ensure_index()
        bulk_lines: list[str] = []
        for record in records:
            chunk_id = str(record.chunk.chunk_id)
            external_id = _external_record_id(record)
            bulk_lines.append(
                json.dumps(
                    {"index": {"_index": self.target_name, "_id": external_id}},
                    separators=(",", ":"),
                )
            )
            bulk_lines.append(
                json.dumps(
                    {
                        "tenant_id": str(record.tenant_id),
                        "collection_id": str(record.collection_id),
                        "document_version_id": str(record.document_version_id),
                        "chunk_id": chunk_id,
                        "publication_job_id": str(record.publication_job_id),
                        "publication_attempt": record.publication_attempt,
                        "body": record.chunk.text,
                        "page_start": record.chunk.page_start,
                        "page_end": record.chunk.page_end,
                        "section_path": record.chunk.section_path,
                        "content_sha256": record.chunk.content_sha256,
                        "pipeline_profile": record.pipeline_profile,
                        "chunker_name": record.chunk.chunker_name,
                        "chunker_version": record.chunk.chunker_version,
                        "embedding_provider": record.embedding_provider,
                        "embedding_model": record.embedding_model,
                        "embedding_model_version": record.embedding_model_version,
                        "publication_complete": False,
                        "embedding": record.embedding.vector,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        response = await self._request(
            "POST",
            "/_bulk?refresh=wait_for",
            content=("\n".join(bulk_lines) + "\n").encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        payload = response.json()
        if payload.get("errors"):
            raise SearchPublicationError("OpenSearch bulk publication was incomplete")
        await self._verify_batch(records)
        publications: list[PublicationRecord] = []
        for record in records:
            external_id = _external_record_id(record)
            publications.extend(
                (
                    PublicationRecord(
                        chunk_id=record.chunk.chunk_id,
                        publication_kind="lexical",
                        external_record_id=f"{external_id}:lexical",
                    ),
                    PublicationRecord(
                        chunk_id=record.chunk.chunk_id,
                        publication_kind="vector",
                        external_record_id=f"{external_id}:vector",
                    ),
                )
            )
        return publications

    async def activate_version(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        expected_records: int,
        target: SearchTarget | None = None,
        publication_job_id: UUID | None = None,
        publication_attempt: int | None = None,
    ) -> None:
        await self._set_version_visibility(
            tenant_id,
            collection_id,
            document_version_id,
            expected_records,
            target,
            publication_job_id,
            publication_attempt,
            visible=True,
        )

    async def deactivate_version(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        expected_records: int,
        target: SearchTarget | None = None,
        publication_job_id: UUID | None = None,
        publication_attempt: int | None = None,
    ) -> None:
        await self._set_version_visibility(
            tenant_id,
            collection_id,
            document_version_id,
            expected_records,
            target,
            publication_job_id,
            publication_attempt,
            visible=False,
        )

    async def _set_version_visibility(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        expected_records: int,
        target: SearchTarget | None,
        publication_job_id: UUID | None,
        publication_attempt: int | None,
        *,
        visible: bool,
    ) -> None:
        filters: list[dict[str, dict[str, str | int]]] = [
            {"term": {"tenant_id": str(tenant_id)}},
            {"term": {"collection_id": str(collection_id)}},
            {"term": {"document_version_id": str(document_version_id)}},
        ]
        filters.extend(
            _publication_attempt_filters(
                publication_job_id,
                publication_attempt,
            )
        )
        target_name = target.name if target is not None else self.target_name
        response = await self._request(
            "POST",
            f"/{target_name}/_update_by_query?refresh=true&conflicts=proceed",
            json_payload={
                "query": {"bool": {"filter": filters}},
                "script": {
                    "lang": "painless",
                    "source": (
                        "ctx._source.publication_complete = params.visible"
                    ),
                    "params": {"visible": visible},
                },
            },
        )
        payload = response.json()
        if (
            payload.get("version_conflicts")
            or payload.get("failures")
            or payload.get("total") != expected_records
            or int(payload.get("updated", 0)) + int(payload.get("noops", 0))
            != expected_records
        ):
            operation = "activation" if visible else "deactivation"
            raise SearchPublicationError(f"OpenSearch {operation} was incomplete")

    async def delete_version(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        target: SearchTarget | None = None,
        publication_job_id: UUID | None = None,
        publication_attempt: int | None = None,
    ) -> int:
        """Remove every lexical/vector record in one exact tenant scope."""
        target_name = target.name if target is not None else self.target_name
        filters: list[dict[str, dict[str, str | int]]] = [
            {"term": {"tenant_id": str(tenant_id)}},
            {"term": {"collection_id": str(collection_id)}},
            {"term": {"document_version_id": str(document_version_id)}},
        ]
        filters.extend(
            _publication_attempt_filters(
                publication_job_id,
                publication_attempt,
            )
        )
        response = await self._raw_request(
            "POST",
            f"/{target_name}/_delete_by_query?refresh=true&conflicts=proceed",
            json_payload={
                "query": {
                    "bool": {
                        "filter": filters
                    }
                }
            },
        )
        if response.status_code == 404:
            return 0
        if response.status_code >= 400:
            self._raise_response(response)
        payload = response.json()
        if (
            payload.get("timed_out")
            or payload.get("version_conflicts")
            or payload.get("failures")
        ):
            raise SearchPublicationError("OpenSearch deletion was incomplete")
        return int(payload.get("deleted", 0))

    async def _verify_batch(self, records: Sequence[SearchRecord]) -> None:
        response = await self._request(
            "POST",
            f"/{self.target_name}/_mget",
            json_payload={
                "ids": [_external_record_id(record) for record in records]
            },
        )
        documents = response.json().get("docs", [])
        expected = {
            _external_record_id(record): (
                str(record.tenant_id),
                str(record.collection_id),
                str(record.document_version_id),
                str(record.publication_job_id),
                record.publication_attempt,
            )
            for record in records
        }
        if len(documents) != len(expected):
            raise SearchPublicationError("OpenSearch verification count mismatch")
        for document in documents:
            source = document.get("_source", {})
            identity = expected.get(str(document.get("_id")))
            actual = (
                source.get("tenant_id"),
                source.get("collection_id"),
                source.get("document_version_id"),
                source.get("publication_job_id"),
                source.get("publication_attempt"),
            )
            if not document.get("found") or identity != actual:
                raise SearchPublicationError("OpenSearch scope verification failed")

def _external_record_id(record: SearchRecord) -> str:
    if record.publication_attempt < 1:
        raise ValueError("publication attempt must be positive")
    return (
        f"{record.publication_job_id}:"
        f"{record.publication_attempt}:"
        f"{record.chunk.chunk_id}"
    )


def _publication_attempt_filters(
    publication_job_id: UUID | None,
    publication_attempt: int | None,
) -> list[dict[str, dict[str, str | int]]]:
    if (publication_job_id is None) != (publication_attempt is None):
        raise ValueError("publication job and attempt must be paired")
    if publication_job_id is None:
        return []
    if publication_attempt is None or publication_attempt < 1:
        raise ValueError("publication attempt must be positive")
    return [
        {"term": {"publication_job_id": str(publication_job_id)}},
        {"term": {"publication_attempt": publication_attempt}},
    ]
