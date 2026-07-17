"""Bounded object-ledger queries for physical document cleanup."""

SEARCH_TARGETS_SQL = """
SELECT target_name, target_version FROM (
    SELECT target_name, target_version
    FROM index_publications
    WHERE tenant_id = :tenant_id AND document_version_id = :version_id
    UNION
    SELECT publication_target_name, publication_target_version
    FROM ingestion_jobs
    WHERE tenant_id = :tenant_id
      AND document_version_id = :version_id
      AND publication_target_name IS NOT NULL
) persisted_targets
ORDER BY target_name, target_version
"""


INITIALIZE_CLEANUP_LEDGER_SQL = """
WITH RECURSIVE retained_artifacts AS (
    SELECT id, source_artifact_id, object_key
    FROM artifacts
    WHERE tenant_id = :tenant_id
      AND collection_id = :collection_id
      AND document_version_id = :version_id
      AND retention_until > now()
    UNION
    SELECT parent.id, parent.source_artifact_id, parent.object_key
    FROM artifacts parent
    JOIN retained_artifacts child ON child.source_artifact_id = parent.id
    WHERE parent.tenant_id = :tenant_id
      AND parent.collection_id = :collection_id
      AND parent.document_version_id = :version_id
)
INSERT INTO object_cleanup_entries (
    id, tenant_id, collection_id, document_version_id,
    lifecycle_request_id, object_key
) SELECT CAST(md5(CAST(:request_id AS text) || candidate.object_key) AS uuid),
         :tenant_id, :collection_id, :version_id,
         :request_id, candidate.object_key
FROM (
    SELECT object_key FROM artifacts
    WHERE tenant_id = :tenant_id
      AND document_version_id = :version_id
      AND (retention_until IS NULL OR retention_until <= now())
    UNION SELECT CAST(:original_key AS varchar)
    WHERE NOT EXISTS (
        SELECT 1 FROM artifacts retained_original
        WHERE retained_original.tenant_id = :tenant_id
          AND retained_original.document_version_id = :version_id
          AND retained_original.object_key = :original_key
          AND retained_original.retention_until > now()
    )
) candidate
WHERE NOT EXISTS (
    SELECT 1 FROM retained_artifacts retained
    WHERE retained.object_key = candidate.object_key
)
AND NOT EXISTS (
    SELECT 1 FROM document_versions other
    JOIN collections other_collection
      ON other_collection.tenant_id = other.tenant_id
     AND other_collection.id = other.collection_id
    WHERE other.tenant_id = :tenant_id
      AND other.id <> :version_id
      AND other.object_key = candidate.object_key
      AND (
        other.state <> 'deleted'
        OR other_collection.legal_hold
        OR now() < other.created_at
            + (other_collection.retention_days * interval '1 day')
        OR EXISTS (
            SELECT 1 FROM artifacts retained
            WHERE retained.tenant_id = other.tenant_id
              AND retained.document_version_id = other.id
              AND retained.object_key = candidate.object_key
              AND retained.retention_until > now()
        )
      )
) ON CONFLICT ON CONSTRAINT uq_object_cleanup_key DO NOTHING
"""


PENDING_CLEANUP_KEYS_SQL = """
WITH RECURSIVE retained_artifacts AS (
    SELECT id, source_artifact_id, object_key
    FROM artifacts
    WHERE tenant_id = :tenant_id
      AND document_version_id = :version_id
      AND retention_until > now()
    UNION
    SELECT parent.id, parent.source_artifact_id, parent.object_key
    FROM artifacts parent
    JOIN retained_artifacts child ON child.source_artifact_id = parent.id
    WHERE parent.tenant_id = :tenant_id
      AND parent.document_version_id = :version_id
)
SELECT DISTINCT cleanup.object_key
FROM object_cleanup_entries cleanup
WHERE cleanup.tenant_id = :tenant_id
  AND cleanup.document_version_id = :version_id
  AND cleanup.state = 'pending'
  AND NOT EXISTS (
    SELECT 1 FROM retained_artifacts retained
    WHERE retained.object_key = cleanup.object_key
  )
ORDER BY cleanup.object_key LIMIT :limit
"""


RECHECK_CLEANUP_KEYS_SQL = """
WITH RECURSIVE retained_artifacts AS (
    SELECT id, source_artifact_id, object_key
    FROM artifacts
    WHERE tenant_id = :tenant_id
      AND document_version_id = :version_id
      AND retention_until > now()
    UNION
    SELECT parent.id, parent.source_artifact_id, parent.object_key
    FROM artifacts parent
    JOIN retained_artifacts child ON child.source_artifact_id = parent.id
    WHERE parent.tenant_id = :tenant_id
      AND parent.document_version_id = :version_id
)
SELECT DISTINCT cleanup.object_key
FROM object_cleanup_entries cleanup
WHERE cleanup.tenant_id = :tenant_id
  AND cleanup.document_version_id = :version_id
  AND cleanup.object_key = ANY(:object_keys)
  AND cleanup.state = 'pending'
  AND NOT EXISTS (
    SELECT 1 FROM retained_artifacts retained
    WHERE retained.object_key = cleanup.object_key
  )
ORDER BY cleanup.object_key
"""


ELIGIBLE_PENDING_COUNT_SQL = """
WITH RECURSIVE retained_artifacts AS (
    SELECT id, source_artifact_id, object_key
    FROM artifacts
    WHERE tenant_id = :tenant_id
      AND document_version_id = :version_id
      AND retention_until > now()
    UNION
    SELECT parent.id, parent.source_artifact_id, parent.object_key
    FROM artifacts parent
    JOIN retained_artifacts child ON child.source_artifact_id = parent.id
    WHERE parent.tenant_id = :tenant_id
      AND parent.document_version_id = :version_id
)
SELECT count(DISTINCT cleanup.object_key)
FROM object_cleanup_entries cleanup
WHERE cleanup.tenant_id = :tenant_id
  AND cleanup.document_version_id = :version_id
  AND cleanup.state = 'pending'
  AND NOT EXISTS (
    SELECT 1 FROM retained_artifacts retained
    WHERE retained.object_key = cleanup.object_key
  )
"""
