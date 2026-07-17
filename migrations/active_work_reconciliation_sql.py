"""Populated-database reconciliation for active-work guard migration."""

RECONCILE_EXISTING_ACTIVE_WORK = """
WITH ranked AS (
    SELECT id, row_number() OVER (
        PARTITION BY tenant_id, collection_id, document_version_id
        ORDER BY created_at, id
    ) AS position
    FROM ingestion_jobs
    WHERE stage = 'delete'
      AND state IN ('pending', 'leased', 'running', 'retry_scheduled')
), duplicate_jobs AS (
    SELECT id FROM ranked WHERE position > 1
), closed_attempts AS (
    UPDATE job_attempts attempt
    SET finished_at = now(), heartbeat_at = now(), outcome = 'cancelled',
        reason_code = 'DUPLICATE_ACTIVE_DELETE_MIGRATION'
    FROM duplicate_jobs duplicate
    WHERE attempt.job_id = duplicate.id AND attempt.finished_at IS NULL
    RETURNING attempt.job_id
)
UPDATE ingestion_jobs job
SET state = 'cancelled', terminal_reason_code =
        'DUPLICATE_ACTIVE_DELETE_MIGRATION',
    lease_owner = NULL, lease_expires_at = NULL, retry_at = NULL,
    updated_at = now()
FROM duplicate_jobs duplicate
LEFT JOIN closed_attempts closed ON closed.job_id = duplicate.id
WHERE job.id = duplicate.id;

UPDATE lifecycle_requests request
SET state = 'failed', completed_at = COALESCE(completed_at, now())
WHERE request.id IN (
    SELECT lifecycle_request_id FROM ingestion_jobs
    WHERE terminal_reason_code = 'DUPLICATE_ACTIVE_DELETE_MIGRATION'
      AND lifecycle_request_id IS NOT NULL
) AND NOT EXISTS (
    SELECT 1 FROM ingestion_jobs active
    WHERE active.lifecycle_request_id = request.id
      AND active.stage = 'delete'
      AND active.state IN ('pending', 'leased', 'running', 'retry_scheduled')
);

WITH target_scores AS (
    SELECT tenant_id, collection_id, document_version_id,
           target_name, target_version,
           count(DISTINCT (chunk_id, publication_kind)) AS evidence_count,
           max(published_at) AS latest_publication,
           count(DISTINCT chunk_id) FILTER (
               WHERE publication_kind = 'lexical'
           ) = (
               SELECT count(*) FROM chunks chunk
               WHERE chunk.tenant_id = publication.tenant_id
                 AND chunk.collection_id = publication.collection_id
                 AND chunk.document_version_id =
                     publication.document_version_id
           ) AND count(DISTINCT chunk_id) FILTER (
               WHERE publication_kind = 'vector'
           ) = (
               SELECT count(*) FROM chunks chunk
               WHERE chunk.tenant_id = publication.tenant_id
                 AND chunk.collection_id = publication.collection_id
                 AND chunk.document_version_id =
                     publication.document_version_id
           ) AS complete
    FROM index_publications publication
    WHERE deleted_at IS NULL
    GROUP BY tenant_id, collection_id, document_version_id,
             target_name, target_version
), selected_targets AS (
    SELECT *, row_number() OVER (
        PARTITION BY tenant_id, collection_id, document_version_id
        ORDER BY complete DESC, evidence_count DESC, latest_publication DESC,
                 target_name, target_version
    ) AS position
    FROM target_scores
), retired_targets AS (
    INSERT INTO search_cleanup_entries (
        id, tenant_id, collection_id, document_version_id,
        target_name, target_version
    ) SELECT CAST(md5(
               CAST(tenant_id AS text) || CAST(collection_id AS text)
               || CAST(document_version_id AS text) || target_name
               || target_version
             ) AS uuid),
             tenant_id, collection_id, document_version_id,
             target_name, target_version
    FROM selected_targets
    WHERE position > 1
    ON CONFLICT ON CONSTRAINT uq_search_cleanup_target DO NOTHING
    RETURNING id
)
UPDATE index_publications publication
SET deleted_at = COALESCE(publication.deleted_at, now())
FROM selected_targets selected
WHERE selected.position > 1
  AND publication.tenant_id = selected.tenant_id
  AND publication.collection_id = selected.collection_id
  AND publication.document_version_id = selected.document_version_id
  AND publication.target_name = selected.target_name
  AND publication.target_version = selected.target_version
  AND publication.deleted_at IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM document_versions version
        WHERE version.state IN ('ready', 'ready_with_warnings')
          AND NOT EXISTS (
            SELECT publication.target_name, publication.target_version
            FROM index_publications publication
            WHERE publication.tenant_id = version.tenant_id
              AND publication.collection_id = version.collection_id
              AND publication.document_version_id = version.id
              AND publication.deleted_at IS NULL
            GROUP BY publication.target_name, publication.target_version
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
          )
    ) THEN
        RAISE EXCEPTION
            'ready version has no complete single search target';
    END IF;
END;
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM index_publications publication
        JOIN artifacts artifact
          ON artifact.tenant_id = publication.tenant_id
         AND artifact.collection_id = publication.collection_id
         AND artifact.document_version_id =
             publication.document_version_id
         AND artifact.artifact_type = 'index_manifest'
         AND artifact.generator_name = 'opensearch'
         AND artifact.generator_version = publication.target_version
        WHERE publication.deleted_at IS NULL
        GROUP BY publication.tenant_id, publication.collection_id,
                 publication.document_version_id,
                 publication.target_name, publication.target_version
        HAVING count(DISTINCT artifact.id) > 1
    ) THEN
        RAISE EXCEPTION
            'legacy search manifest target mapping is ambiguous';
    END IF;
END;
$$;

WITH active_targets AS (
    SELECT DISTINCT tenant_id, collection_id, document_version_id,
           target_name, target_version
    FROM index_publications
    WHERE deleted_at IS NULL
), artifact_targets AS (
    SELECT artifact.id, target.target_name, target.target_version,
           row_number() OVER (
               PARTITION BY target.tenant_id, target.collection_id,
                            target.document_version_id,
                            target.target_name, target.target_version
               ORDER BY artifact.created_at DESC, artifact.id DESC
           ) AS position
    FROM artifacts artifact
    JOIN active_targets target
      ON target.tenant_id = artifact.tenant_id
     AND target.collection_id = artifact.collection_id
     AND target.document_version_id = artifact.document_version_id
     AND target.target_version = artifact.generator_version
    WHERE artifact.artifact_type = 'index_manifest'
      AND artifact.generator_name = 'opensearch'
)
UPDATE artifacts artifact
SET search_target_name = target.target_name,
    search_target_version = target.target_version
FROM artifact_targets target
WHERE artifact.id = target.id AND target.position = 1;

UPDATE index_publications publication
SET activated_at = publication.published_at
FROM document_versions version
WHERE version.tenant_id = publication.tenant_id
  AND version.collection_id = publication.collection_id
  AND version.id = publication.document_version_id
  AND version.state IN ('ready', 'ready_with_warnings')
  AND publication.deleted_at IS NULL;
"""
