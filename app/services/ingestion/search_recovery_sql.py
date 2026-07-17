"""SQL statements used by bounded search activation recovery."""

PENDING_ACTIVATIONS_SQL = """
SELECT publication.tenant_id, publication.collection_id,
       publication.document_version_id,
       publication.target_name,
       publication.target_version,
       publication.publication_job_id,
       publication.publication_attempt,
       count(DISTINCT publication.chunk_id) AS record_count
FROM index_publications publication
JOIN document_versions version
  ON version.tenant_id = publication.tenant_id
 AND version.collection_id = publication.collection_id
 AND version.id = publication.document_version_id
WHERE publication.deleted_at IS NULL
  AND publication.activated_at IS NULL
  AND publication.publication_job_id IS NOT NULL
  AND version.state IN ('ready', 'ready_with_warnings')
  AND (
    CAST(:activation_version_id AS uuid) IS NULL
    OR version.id = :activation_version_id
  )
  AND (
    CAST(:document_version_id AS uuid) IS NULL
    OR version.id = :document_version_id
  )
  AND NOT EXISTS (
    SELECT 1 FROM search_cleanup_entries cleanup
    WHERE cleanup.tenant_id = version.tenant_id
      AND cleanup.collection_id = version.collection_id
      AND cleanup.activation_version_id = version.id
      AND cleanup.state <> 'deleted'
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
GROUP BY publication.tenant_id,
         publication.collection_id,
         publication.document_version_id,
         publication.target_name,
         publication.target_version,
         publication.publication_job_id,
         publication.publication_attempt
HAVING count(DISTINCT publication.chunk_id) FILTER (
         WHERE publication.publication_kind = 'lexical'
       ) = (
         SELECT count(*) FROM chunks chunk
         WHERE chunk.tenant_id = publication.tenant_id
           AND chunk.collection_id = publication.collection_id
           AND chunk.document_version_id = publication.document_version_id
       )
   AND count(DISTINCT publication.chunk_id) FILTER (
         WHERE publication.publication_kind = 'vector'
       ) = (
         SELECT count(*) FROM chunks chunk
         WHERE chunk.tenant_id = publication.tenant_id
           AND chunk.collection_id = publication.collection_id
           AND chunk.document_version_id = publication.document_version_id
       )
ORDER BY min(publication.published_at), publication.document_version_id
LIMIT :limit
"""
