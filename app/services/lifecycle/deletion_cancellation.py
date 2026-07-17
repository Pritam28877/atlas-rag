"""Transactional cancellation fence for deletion and dependent reprocessing."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def cancel_competing_jobs(
    session: AsyncSession,
    tenant_id: UUID,
    version_id: UUID,
    deletion_job_id: UUID,
) -> None:
    await session.execute(
        text(
            """
            WITH RECURSIVE affected_versions AS (
                SELECT id FROM document_versions
                WHERE tenant_id = :tenant_id AND id = :version_id
                UNION ALL
                SELECT child.id FROM document_versions child
                JOIN affected_versions parent
                  ON child.reprocessed_from_version_id = parent.id
                WHERE child.tenant_id = :tenant_id
            ), cancelled AS (
                UPDATE ingestion_jobs job
                SET state = 'cancelled', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = now()
                FROM affected_versions affected
                WHERE job.tenant_id = :tenant_id
                  AND job.document_version_id = affected.id
                  AND job.id <> :deletion_job_id
                  AND job.state IN (
                    'pending', 'leased', 'running', 'retry_scheduled'
                  )
                RETURNING job.id, job.attempt_count,
                          job.lifecycle_request_id
            ), closed_attempts AS (
                UPDATE job_attempts attempt
                SET finished_at = now(), heartbeat_at = now(),
                    outcome = 'cancelled', reason_code = 'LIFECYCLE_DELETE_FENCE'
                FROM cancelled
                WHERE attempt.tenant_id = :tenant_id
                  AND attempt.job_id = cancelled.id
                  AND attempt.attempt_number = cancelled.attempt_count
                  AND attempt.finished_at IS NULL
                RETURNING attempt.id
            )
            UPDATE lifecycle_requests request
            SET state = 'failed', completed_at = now()
            FROM cancelled
            WHERE request.id = cancelled.lifecycle_request_id
              AND request.state IN ('pending', 'running')
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": version_id,
            "deletion_job_id": deletion_job_id,
        },
    )
    await session.execute(
        text(
            """
            WITH RECURSIVE descendants AS (
                SELECT child.id
                FROM document_versions child
                WHERE child.tenant_id = :tenant_id
                  AND child.reprocessed_from_version_id = :version_id
                UNION ALL
                SELECT child.id FROM document_versions child
                JOIN descendants parent
                  ON child.reprocessed_from_version_id = parent.id
                WHERE child.tenant_id = :tenant_id
            ), candidates AS MATERIALIZED (
                SELECT version.id, version.collection_id, version.state,
                       version.state_revision
                FROM document_versions version
                JOIN descendants ON descendants.id = version.id
                WHERE version.state NOT IN (
                  'ready', 'ready_with_warnings', 'deduplicated', 'rejected',
                  'failed', 'quarantined', 'superseded', 'cancelled', 'deleted'
                )
                FOR UPDATE OF version
            ), updated AS (
                UPDATE document_versions version
                SET state = 'cancelled',
                    state_revision = candidates.state_revision + 1,
                    updated_at = now()
                FROM candidates WHERE version.id = candidates.id
                RETURNING version.id
            )
            INSERT INTO version_transition_events (
                id, tenant_id, collection_id, document_version_id,
                idempotency_key, from_state, to_state, from_revision,
                to_revision, operation, actor_type, actor_id
            ) SELECT CAST(md5(CAST(:deletion_job_id AS text)
                || CAST(candidates.id AS text)) AS uuid),
                :tenant_id, candidates.collection_id, candidates.id,
                'delete-fence:' || CAST(:deletion_job_id AS text),
                candidates.state, 'cancelled', candidates.state_revision,
                candidates.state_revision + 1, 'cancel', 'worker', 'deletion-fence'
            FROM candidates JOIN updated ON updated.id = candidates.id
            ON CONFLICT ON CONSTRAINT uq_version_transition_idempotency DO NOTHING
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": version_id,
            "deletion_job_id": deletion_job_id,
        },
    )
