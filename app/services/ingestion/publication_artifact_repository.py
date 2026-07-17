"""Chunk and embedding catalog persistence for publication jobs."""

import json
from collections.abc import Iterable
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog.enums import VersionState
from app.services.ingestion.chunking import ChunkRecord
from app.services.ingestion.publication_claim import PublicationClaimRepository
from app.services.ingestion.publication_models import (
    ClaimedPublicationJob,
    EmbeddingRecord,
    SearchTarget,
)
from app.services.ingestion.repository_support import NextStageJob, PublishedArtifact


class PublicationArtifactRepository(PublicationClaimRepository):
    """Persist bounded chunk and embedding manifests before index publication."""

    async def succeed_chunk(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        worker_id: str,
        artifact: PublishedArtifact,
        chunks: Iterable[ChunkRecord],
        max_attempts: int,
    ) -> NextStageJob:
        locked = await self._locked_job_version(session, job, worker_id)
        source_artifact_id = (
            job.normalized_artifact_id
            if job.source_document_version_id == job.document_version_id
            else None
        )
        await self._insert_derived_artifact(
            session, job, artifact, source_artifact_id
        )
        artifact_id = await session.scalar(
            text(
                """
                SELECT id FROM artifacts WHERE tenant_id = :tenant_id
                  AND collection_id = :collection_id
                  AND document_version_id = :version_id
                  AND artifact_type = 'chunk_manifest'
                  AND generator_name = :generator_name
                  AND generator_version = :generator_version
                  AND checksum_sha256 = :checksum
                """
            ),
            {
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
                "generator_name": artifact.generator_name,
                "generator_version": artifact.generator_version,
                "checksum": artifact.checksum_sha256,
            },
        )
        chunk_count = 0
        for chunk in chunks:
            await self._insert_chunk(
                session, job, artifact_id, artifact.object_key, chunk
            )
            chunk_count += 1
        if chunk_count == 0:
            raise ValueError("chunk manifest contains no chunks")
        await self._transition(
            session,
            locked,
            VersionState.EMBEDDING,
            worker_id,
            f"worker:{job.id}:embedding",
        )
        next_id = await self._create_next_job(session, job, "embed", max_attempts)
        await self._complete_job(session, job)
        return NextStageJob(id=next_id, stage="embed")

    async def succeed_embedding(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        worker_id: str,
        artifact: PublishedArtifact,
        vector_object_key: str,
        provider: str,
        model_name: str,
        model_version: str,
        dimensions: int,
        records: Iterable[EmbeddingRecord],
        max_attempts: int,
    ) -> NextStageJob:
        locked = await self._locked_job_version(session, job, worker_id)
        chunk_artifact_id = await self._chunk_artifact_id(session, job)
        await self._insert_derived_artifact(
            session, job, artifact, chunk_artifact_id
        )
        count = 0
        for record in records:
            await session.execute(
                text(
                    """
                    INSERT INTO chunk_embeddings (
                        id, tenant_id, collection_id, document_version_id,
                        chunk_id, provider, model_name, model_version, dimensions,
                        vector_object_key, vector_checksum_sha256
                    ) VALUES (
                        :id, :tenant_id, :collection_id, :version_id,
                        :chunk_id, :provider, :model_name, :model_version,
                        :dimensions, :object_key, :checksum
                    ) ON CONFLICT ON CONSTRAINT uq_chunk_embeddings_model
                    DO NOTHING
                    """
                ),
                {
                    "id": uuid4(),
                    "tenant_id": job.tenant_id,
                    "collection_id": job.collection_id,
                    "version_id": job.document_version_id,
                    "chunk_id": record.chunk_id,
                    "provider": provider,
                    "model_name": model_name,
                    "model_version": model_version,
                    "dimensions": dimensions,
                    "object_key": vector_object_key,
                    "checksum": record.checksum_sha256,
                },
            )
            count += 1
        await self._require_exact_count(session, job, "chunk_embeddings", count)
        await self._transition(
            session,
            locked,
            VersionState.INDEXING,
            worker_id,
            f"worker:{job.id}:indexing",
        )
        next_id = await self._create_next_job(session, job, "index", max_attempts)
        await self._complete_job(session, job)
        return NextStageJob(id=next_id, stage="index")

    async def _insert_chunk(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        artifact_id: UUID,
        object_key: str,
        chunk: ChunkRecord,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO chunks (
                    id, tenant_id, collection_id, document_version_id,
                    source_artifact_id, ordinal, page_start, page_end,
                    content_sha256, content_object_key, token_count,
                    section_path, block_bounds, chunker_name, chunker_version
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :artifact_id, :ordinal, :page_start, :page_end,
                    :checksum, :object_key, :token_count,
                    CAST(:section_path AS jsonb), CAST(:block_bounds AS jsonb),
                    :chunker_name, :chunker_version
                ) ON CONFLICT ON CONSTRAINT uq_chunks_version_id DO NOTHING
                """
            ),
            {
                "id": chunk.chunk_id,
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
                "artifact_id": artifact_id,
                "ordinal": chunk.ordinal,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "checksum": chunk.content_sha256,
                "object_key": object_key,
                "token_count": chunk.token_count,
                "section_path": json.dumps(chunk.section_path),
                "block_bounds": json.dumps(chunk.block_bounds),
                "chunker_name": chunk.chunker_name,
                "chunker_version": chunk.chunker_version,
            },
        )

    async def _require_exact_count(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        table: str,
        expected: int,
    ) -> None:
        if table != "chunk_embeddings":
            raise ValueError("unsupported reconciliation table")
        actual = await session.scalar(
            text(
                """
                SELECT count(*) FROM chunk_embeddings
                WHERE tenant_id = :tenant_id AND collection_id = :collection_id
                  AND document_version_id = :version_id
                """
            ),
            {
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
            },
        )
        if actual != expected:
            raise ValueError("embedding catalog count does not match manifest")

    async def _chunk_artifact_id(
        self, session: AsyncSession, job: ClaimedPublicationJob
    ) -> UUID:
        artifact_id = await session.scalar(
            text(
                """
                SELECT id FROM artifacts WHERE tenant_id = :tenant_id
                  AND collection_id = :collection_id
                  AND document_version_id = :version_id
                  AND artifact_type = 'chunk_manifest'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ),
            {
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
            },
        )
        if not isinstance(artifact_id, UUID):
            raise ValueError("chunk artifact is unavailable")
        return artifact_id

    async def _insert_derived_artifact(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        artifact: PublishedArtifact,
        source_artifact_id: UUID | None,
        search_target: SearchTarget | None = None,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, tenant_id, collection_id, document_version_id,
                    source_artifact_id, artifact_type, object_key,
                    generator_name, generator_version, checksum_sha256,
                    size_bytes, search_target_name, search_target_version
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :source_id, :artifact_type, :object_key,
                    :generator_name, :generator_version, :checksum,
                    :size_bytes, :search_target_name, :search_target_version
                ) ON CONFLICT ON CONSTRAINT uq_artifacts_output DO NOTHING
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
                "source_id": source_artifact_id,
                "artifact_type": artifact.artifact_type,
                "object_key": artifact.object_key,
                "generator_name": artifact.generator_name,
                "generator_version": artifact.generator_version,
                "checksum": artifact.checksum_sha256,
                "size_bytes": artifact.size_bytes,
                "search_target_name": (
                    search_target.name if search_target is not None else None
                ),
                "search_target_version": (
                    search_target.version if search_target is not None else None
                ),
            },
        )
