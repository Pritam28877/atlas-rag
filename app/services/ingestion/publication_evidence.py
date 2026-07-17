"""Exact database evidence required before a version becomes searchable."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog.state_machine import PublicationEvidence
from app.services.ingestion.publication_models import (
    ClaimedPublicationJob,
    SearchTarget,
)


async def require_publication_evidence(
    session: AsyncSession,
    job: ClaimedPublicationJob,
    target: SearchTarget,
) -> PublicationEvidence:
    counts = (
        await session.execute(
            text(
                """
                SELECT count(DISTINCT chunk.id) AS chunks,
                  count(DISTINCT embedding.id) AS embeddings,
                  count(DISTINCT publication.id) FILTER (
                    WHERE publication.publication_kind = 'lexical'
                      AND publication.deleted_at IS NULL
                  ) AS lexical,
                  count(DISTINCT publication.id) FILTER (
                    WHERE publication.publication_kind = 'vector'
                      AND publication.deleted_at IS NULL
                  ) AS vector
                FROM chunks chunk
                LEFT JOIN chunk_embeddings embedding
                  ON embedding.tenant_id = chunk.tenant_id
                 AND embedding.collection_id = chunk.collection_id
                 AND embedding.chunk_id = chunk.id
                LEFT JOIN index_publications publication
                 ON publication.tenant_id = chunk.tenant_id
                 AND publication.collection_id = chunk.collection_id
                 AND publication.chunk_id = chunk.id
                 AND publication.target_name = :target_name
                 AND publication.target_version = :target_version
                 AND publication.publication_job_id = :publication_job_id
                 AND publication.publication_attempt = :publication_attempt
                WHERE chunk.tenant_id = :tenant_id
                  AND chunk.collection_id = :collection_id
                  AND chunk.document_version_id = :version_id
                """
            ),
            {
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
                "target_name": target.name,
                "target_version": target.version,
                "publication_job_id": job.id,
                "publication_attempt": job.attempt_number,
            },
        )
    ).mappings().one()
    expected = int(str(counts["chunks"]))
    if expected < 1 or any(
        int(str(counts[name])) != expected
        for name in ("embeddings", "lexical", "vector")
    ):
        raise ValueError("index publication evidence is incomplete")
    manifests = (
        await session.execute(
            text(
                """
                SELECT
                  EXISTS(SELECT 1 FROM artifacts WHERE tenant_id = :tenant_id
                    AND collection_id = :collection_id
                    AND document_version_id = :version_id
                    AND artifact_type = 'chunk_manifest') AS chunk_manifest,
                  EXISTS(SELECT 1 FROM artifacts WHERE tenant_id = :tenant_id
                    AND collection_id = :collection_id
                    AND document_version_id = :version_id
                    AND artifact_type = 'index_manifest'
                    AND generator_name = 'opensearch'
                    AND search_target_name = :target_name
                    AND search_target_version = :target_version
                  ) AS index_manifest
                """
            ),
            {
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
                "target_name": target.name,
                "target_version": target.version,
            },
        )
    ).mappings().one()
    return PublicationEvidence(
        chunk_count=expected,
        embedded_chunk_count=int(str(counts["embeddings"])),
        lexical_publication_count=int(str(counts["lexical"])),
        vector_publication_count=int(str(counts["vector"])),
        chunk_manifest_published=bool(manifests["chunk_manifest"]),
        index_manifest_published=bool(manifests["index_manifest"]),
    )
