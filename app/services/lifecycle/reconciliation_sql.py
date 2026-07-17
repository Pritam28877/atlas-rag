"""Bounded catalog and job scans used by lifecycle reconciliation."""

STALE_JOB_SCAN_SQL = """
WITH candidates AS MATERIALIZED (
    SELECT job.id, job.tenant_id, job.collection_id,
           job.document_version_id, job.stage, job.state,
           job.attempt_count,
           CASE
             WHEN job.state IN ('leased', 'running')
               AND job.lease_expires_at < now()
               THEN 'expired_lease'
             ELSE 'stale_dispatch'
           END AS reason
    FROM ingestion_jobs job
    WHERE (
        job.state IN ('leased', 'running')
        AND job.lease_expires_at < now()
    ) OR (
        job.state = 'pending'
        AND job.updated_at < now() - :stale_after
    ) OR (
        job.state = 'retry_scheduled'
        AND job.retry_at IS NOT NULL
        AND job.retry_at <= now()
    )
    ORDER BY job.updated_at, job.id
    LIMIT :page_size FOR UPDATE SKIP LOCKED
), closed_attempts AS (
    UPDATE job_attempts attempt
    SET finished_at = now(), heartbeat_at = now(),
        outcome = 'lease_expired', error_class = 'internal',
        reason_code = 'WORKER_LEASE_EXPIRED'
    FROM candidates
    WHERE candidates.reason = 'expired_lease'
      AND attempt.tenant_id = candidates.tenant_id
      AND attempt.job_id = candidates.id
      AND attempt.attempt_number = candidates.attempt_count
      AND attempt.finished_at IS NULL
    RETURNING attempt.job_id
)
UPDATE ingestion_jobs job
SET state = 'pending', lease_owner = NULL,
    lease_expires_at = NULL, retry_at = NULL,
    updated_at = now(),
    sanitized_context = jsonb_build_object(
        'reconciliation_reason', candidates.reason
    )
FROM candidates
LEFT JOIN closed_attempts ON closed_attempts.job_id = candidates.id
WHERE job.id = candidates.id
RETURNING job.id, job.tenant_id, job.document_version_id,
          job.stage, candidates.reason
"""


CATALOG_FINDINGS_SQL = """
SELECT version.tenant_id, version.collection_id,
       version.id AS version_id, NULL::uuid AS job_id,
       NULL::varchar AS stage, version.state AS version_state,
       CASE
         WHEN version.state = 'deleted' THEN 'retention_cleanup_due'
         WHEN NOT EXISTS (
           SELECT 1 FROM artifacts artifact
           WHERE artifact.tenant_id = version.tenant_id
             AND artifact.document_version_id = version.id
             AND artifact.artifact_type = 'chunk_manifest'
         ) THEN 'missing_chunk_manifest'
         WHEN NOT EXISTS (
           SELECT 1 FROM artifacts artifact
           WHERE artifact.tenant_id = version.tenant_id
             AND artifact.document_version_id = version.id
             AND artifact.artifact_type = 'index_manifest'
             AND artifact.generator_name = 'opensearch'
         ) THEN 'missing_index_manifest'
         WHEN EXISTS (
           SELECT 1 FROM chunks chunk
           WHERE chunk.tenant_id = version.tenant_id
             AND chunk.document_version_id = version.id
             AND NOT EXISTS (
               SELECT 1 FROM chunk_embeddings embedding
               WHERE embedding.tenant_id = chunk.tenant_id
                 AND embedding.collection_id = chunk.collection_id
                 AND embedding.document_version_id = chunk.document_version_id
                 AND embedding.chunk_id = chunk.id
             )
         ) THEN 'missing_embedding'
         ELSE 'incomplete_publication'
       END AS reason
FROM document_versions version
JOIN collections collection
  ON collection.tenant_id = version.tenant_id
 AND collection.id = version.collection_id
WHERE (
    version.state = 'deleted' AND NOT collection.legal_hold
    AND now() >= version.created_at
        + (collection.retention_days * interval '1 day')
    AND (
        EXISTS (
          SELECT 1 FROM artifacts artifact
          WHERE artifact.tenant_id = version.tenant_id
            AND artifact.document_version_id = version.id
            AND (
              artifact.retention_until IS NULL
              OR artifact.retention_until <= now()
            )
        ) OR (
          NOT EXISTS (
            SELECT 1 FROM artifacts original
            WHERE original.tenant_id = version.tenant_id
              AND original.document_version_id = version.id
              AND original.object_key = version.object_key
          ) AND NOT EXISTS (
            SELECT 1 FROM object_cleanup_entries cleaned
            WHERE cleaned.tenant_id = version.tenant_id
              AND cleaned.document_version_id = version.id
              AND cleaned.object_key = version.object_key
              AND cleaned.state = 'deleted'
          )
        )
    )
    AND NOT EXISTS (
        SELECT 1 FROM ingestion_jobs active
        WHERE active.tenant_id = version.tenant_id
          AND active.document_version_id = version.id
          AND active.stage = 'delete'
          AND active.state IN ('pending', 'leased', 'running', 'retry_scheduled')
    )
    AND NOT EXISTS (
        WITH RECURSIVE descendants AS (
            SELECT child.id
            FROM document_versions child
            WHERE child.tenant_id = version.tenant_id
              AND child.collection_id = version.collection_id
              AND child.reprocessed_from_version_id = version.id
            UNION
            SELECT child.id
            FROM document_versions child
            JOIN descendants parent
              ON child.reprocessed_from_version_id = parent.id
            WHERE child.tenant_id = version.tenant_id
              AND child.collection_id = version.collection_id
        )
        SELECT 1 FROM document_versions descendant
        JOIN descendants ON descendants.id = descendant.id
        WHERE descendant.state <> 'deleted'
    )
) OR (
    version.state IN ('ready', 'ready_with_warnings')
    AND (
        NOT EXISTS (
          SELECT 1 FROM artifacts artifact
          WHERE artifact.tenant_id = version.tenant_id
            AND artifact.document_version_id = version.id
            AND artifact.artifact_type = 'chunk_manifest'
        ) OR NOT EXISTS (
          SELECT 1 FROM artifacts artifact
          WHERE artifact.tenant_id = version.tenant_id
            AND artifact.document_version_id = version.id
            AND artifact.artifact_type = 'index_manifest'
            AND artifact.generator_name = 'opensearch'
        ) OR EXISTS (
          SELECT 1 FROM chunks chunk
          WHERE chunk.tenant_id = version.tenant_id
            AND chunk.document_version_id = version.id
            AND NOT EXISTS (
              SELECT 1 FROM chunk_embeddings embedding
              WHERE embedding.tenant_id = chunk.tenant_id
                AND embedding.collection_id = chunk.collection_id
                AND embedding.document_version_id = chunk.document_version_id
                AND embedding.chunk_id = chunk.id
            )
        ) OR NOT EXISTS (
          SELECT publication.target_name,
                 publication.target_version,
                 publication.publication_job_id,
                 publication.publication_attempt
          FROM index_publications publication
          WHERE publication.tenant_id = version.tenant_id
            AND publication.collection_id = version.collection_id
            AND publication.document_version_id = version.id
            AND publication.deleted_at IS NULL
          GROUP BY publication.target_name,
                   publication.target_version,
                   publication.publication_job_id,
                   publication.publication_attempt
          HAVING EXISTS (
              SELECT 1 FROM artifacts manifest
              WHERE manifest.tenant_id = version.tenant_id
                AND manifest.collection_id = version.collection_id
                AND manifest.document_version_id = version.id
                AND manifest.artifact_type = 'index_manifest'
                AND manifest.generator_name = 'opensearch'
                AND manifest.search_target_name = publication.target_name
                AND manifest.search_target_version = publication.target_version
            )
            AND count(DISTINCT publication.chunk_id) FILTER (
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
        )
    )
)
ORDER BY CASE WHEN version.state = 'deleted' THEN 0 ELSE 1 END,
         version.updated_at, version.id
LIMIT :limit FOR UPDATE OF version SKIP LOCKED
"""
