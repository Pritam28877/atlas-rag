"""Catalog readiness query kept separate from repository orchestration."""

VERSION_STATUS_SQL = """
SELECT version.*,
       job.stage,
       EXISTS(SELECT 1 FROM artifacts artifact
         WHERE artifact.tenant_id = version.tenant_id
           AND artifact.collection_id = version.collection_id
           AND artifact.document_version_id = version.id)
         AS artifacts_available,
       EXISTS(SELECT 1 FROM chunks chunk
         WHERE chunk.tenant_id = version.tenant_id
           AND chunk.collection_id = version.collection_id
           AND chunk.document_version_id = version.id)
         AS citations_ready,
       publication_state.ready AS lexical_index_ready,
       publication_state.ready AS vector_index_ready
FROM document_versions version
LEFT JOIN LATERAL (
    SELECT stage FROM ingestion_jobs
    WHERE tenant_id = version.tenant_id
      AND collection_id = version.collection_id
      AND document_version_id = version.id
    ORDER BY created_at DESC LIMIT 1
) job ON true
LEFT JOIN LATERAL (
    SELECT EXISTS (
        SELECT publication.target_name,
               publication.target_version,
               publication.publication_job_id,
               publication.publication_attempt
        FROM index_publications publication
        WHERE version.state IN ('ready', 'ready_with_warnings')
          AND publication.tenant_id = version.tenant_id
          AND publication.collection_id = version.collection_id
          AND publication.document_version_id = version.id
          AND publication.deleted_at IS NULL
          AND publication.activated_at IS NOT NULL
          AND publication.publication_job_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM artifacts manifest
            WHERE manifest.tenant_id = version.tenant_id
              AND manifest.collection_id = version.collection_id
              AND manifest.document_version_id = version.id
              AND manifest.artifact_type = 'index_manifest'
              AND manifest.generator_name = 'opensearch'
              AND manifest.search_target_name = publication.target_name
              AND manifest.search_target_version = publication.target_version
          )
        GROUP BY publication.target_name,
                 publication.target_version,
                 publication.publication_job_id,
                 publication.publication_attempt
        HAVING count(DISTINCT publication.chunk_id) FILTER (
                  WHERE publication.publication_kind = 'lexical'
               ) = (
                  SELECT count(*) FROM chunks chunk
                  WHERE chunk.tenant_id = version.tenant_id
                    AND chunk.collection_id = version.collection_id
                    AND chunk.document_version_id = version.id
               )
           AND count(DISTINCT publication.chunk_id) FILTER (
                  WHERE publication.publication_kind = 'vector'
               ) = (
                  SELECT count(*) FROM chunks chunk
                  WHERE chunk.tenant_id = version.tenant_id
                    AND chunk.collection_id = version.collection_id
                    AND chunk.document_version_id = version.id
               )
           AND NOT EXISTS (
              SELECT 1 FROM chunks chunk
              WHERE chunk.tenant_id = version.tenant_id
                AND chunk.collection_id = version.collection_id
                AND chunk.document_version_id = version.id
                AND NOT EXISTS (
                  SELECT 1 FROM chunk_embeddings embedding
                  WHERE embedding.tenant_id = chunk.tenant_id
                    AND embedding.collection_id = chunk.collection_id
                    AND embedding.document_version_id = chunk.document_version_id
                    AND embedding.chunk_id = chunk.id
                )
           )
    ) AS ready
) publication_state ON true
WHERE version.tenant_id = :tenant_id AND version.id = :version_id
"""
