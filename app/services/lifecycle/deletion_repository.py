"""Lease-fenced deletion state and durable object cleanup batches."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ingestion.publication_models import SearchTargetPublication
from app.services.ingestion.search_recovery import SearchRecoveryRepository
from app.services.lifecycle.deletion_cancellation import cancel_competing_jobs
from app.services.lifecycle.deletion_models import ClaimedDeletion
from app.services.lifecycle.deletion_repository_support import (
    DeletionRepositorySupport,
)


class DeletionRepository(DeletionRepositorySupport):
    async def claim(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        version_id: UUID,
        job_id: UUID,
        worker_id: str,
        key_limit: int,
    ) -> ClaimedDeletion | None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT job.*, version.state AS version_state,
                           version.state_revision, version.object_key,
                           version.created_at, collection.legal_hold,
                           collection.retention_days, request.id AS request_id
                    FROM ingestion_jobs job
                    JOIN document_versions version
                      ON version.tenant_id = job.tenant_id
                     AND version.collection_id = job.collection_id
                     AND version.id = job.document_version_id
                    JOIN collections collection
                      ON collection.tenant_id = version.tenant_id
                     AND collection.id = version.collection_id
                    JOIN lifecycle_requests request
                      ON request.tenant_id = job.tenant_id
                     AND request.collection_id = job.collection_id
                     AND request.id = job.lifecycle_request_id
                     AND request.document_version_id = version.id
                     AND request.request_type = 'delete'
                    WHERE job.id = :job_id AND job.tenant_id = :tenant_id
                      AND job.document_version_id = :version_id
                      AND job.stage = 'delete'
                    FOR UPDATE OF job, version, request
                    """
                ),
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "version_id": version_id,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            raise ValueError("deletion job identity is invalid")
        if row["state"] == "succeeded":
            await self._complete_request(session, UUID(str(row["request_id"])))
            return None
        if row["state"] in {"cancelled", "failed", "dead_lettered"}:
            return None
        if await self._active_lease(session, row):
            return None
        attempt_number = int(str(row["attempt_count"])) + 1
        if attempt_number > int(str(row["max_attempts"])):
            await self._fail(session, row)
            return None
        await self._start_attempt(session, row, worker_id, attempt_number)
        await cancel_competing_jobs(session, tenant_id, version_id, job_id)
        search_targets = await self._search_targets(session, tenant_id, version_id)
        deletion = ClaimedDeletion(
            job_id=job_id,
            tenant_id=tenant_id,
            collection_id=UUID(str(row["collection_id"])),
            document_version_id=version_id,
            request_id=UUID(str(row["request_id"])),
            attempt_number=attempt_number,
            physical_cleanup_due=False,
            object_keys=(),
            search_targets=search_targets,
        )
        recovery = SearchRecoveryRepository()
        for target in search_targets:
            await recovery.enqueue_cleanup(
                session,
                deletion.tenant_id,
                deletion.collection_id,
                deletion.document_version_id,
                SearchTargetPublication(target=target, record_count=1),
                None,
            )
        await self._mark_search_revoked(session, deletion)
        await self._mark_version_deleted(session, deletion, worker_id, row)
        return deletion

    async def search_cleanup_complete(
        self,
        session: AsyncSession,
        deletion: ClaimedDeletion,
        worker_id: str,
    ) -> bool:
        await self._lock_fence(session, deletion, worker_id)
        unfinished = await session.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM search_cleanup_entries
                    WHERE tenant_id = :tenant_id
                      AND collection_id = :collection_id
                      AND document_version_id = :version_id
                      AND state <> 'deleted'
                )
                """
            ),
            {
                "tenant_id": deletion.tenant_id,
                "collection_id": deletion.collection_id,
                "version_id": deletion.document_version_id,
            },
        )
        return not bool(unfinished)

    async def next_batch(
        self,
        session: AsyncSession,
        deletion: ClaimedDeletion,
        worker_id: str,
        key_limit: int,
    ) -> ClaimedDeletion:
        policy = await self._lock_fence(session, deletion, worker_id)
        cleanup_due = await self._cleanup_due(session, policy)
        if cleanup_due:
            await self._initialize_cleanup_ledger(session, policy)
            keys = await self._pending_keys_for_deletion(
                session, deletion, key_limit
            )
        else:
            keys = ()
        return ClaimedDeletion(
            job_id=deletion.job_id,
            tenant_id=deletion.tenant_id,
            collection_id=deletion.collection_id,
            document_version_id=deletion.document_version_id,
            request_id=deletion.request_id,
            attempt_number=deletion.attempt_number,
            physical_cleanup_due=cleanup_due,
            object_keys=keys,
            search_targets=deletion.search_targets,
        )

    async def complete_batch(
        self,
        session: AsyncSession,
        deletion: ClaimedDeletion,
        worker_id: str,
    ) -> bool:
        version = await self._lock_fence(session, deletion, worker_id)
        if deletion.object_keys:
            await session.execute(
                text(
                    """
                    UPDATE object_cleanup_entries
                    SET state = 'deleted', deleted_at = now(),
                        attempt_count = attempt_count + 1, updated_at = now()
                    WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                      AND object_key = ANY(:object_keys) AND state = 'pending'
                    """
                ),
                {
                    "tenant_id": deletion.tenant_id,
                    "version_id": deletion.document_version_id,
                    "object_keys": list(deletion.object_keys),
                },
            )
        await self._mark_search_revoked(session, deletion)
        await self._mark_version_deleted(session, deletion, worker_id, version)
        if deletion.physical_cleanup_due:
            pending = await self._eligible_pending_count(session, deletion)
            if pending > 0:
                return True
        if deletion.physical_cleanup_due:
            await session.execute(
                text(
                    """
                    DELETE FROM chunks WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                    """
                ),
                {
                    "tenant_id": deletion.tenant_id,
                    "version_id": deletion.document_version_id,
                },
            )
            await session.execute(
                text(
                    """
                    DELETE FROM artifacts artifact
                    WHERE artifact.tenant_id = :tenant_id
                      AND artifact.document_version_id = :version_id
                      AND EXISTS (
                        SELECT 1 FROM object_cleanup_entries cleanup
                        WHERE cleanup.tenant_id = artifact.tenant_id
                          AND cleanup.document_version_id =
                              artifact.document_version_id
                          AND cleanup.object_key = artifact.object_key
                          AND cleanup.state = 'deleted'
                      )
                    """
                ),
                {
                    "tenant_id": deletion.tenant_id,
                    "version_id": deletion.document_version_id,
                },
            )
        await self._finish_attempt(session, deletion, "succeeded", None)
        await self._complete_request(session, deletion.request_id)
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs SET state = 'succeeded', lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = now(), updated_at = now()
                WHERE id = :job_id AND state = 'running'
                  AND lease_owner = :worker_id
                """
            ),
            {"job_id": deletion.job_id, "worker_id": worker_id},
        )
        return False

    async def schedule_retry(
        self,
        session: AsyncSession,
        deletion: ClaimedDeletion,
        worker_id: str,
        reason_code: str,
        delay_seconds: int,
    ) -> None:
        await self._lock_fence(session, deletion, worker_id)
        await self._finish_attempt(session, deletion, "retryable_failure", reason_code)
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs SET state = 'retry_scheduled',
                    retry_at = now() + (:delay_seconds * interval '1 second'),
                    terminal_reason_code = :reason_code, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = now()
                WHERE id = :job_id AND state = 'running'
                  AND lease_owner = :worker_id
                """
            ),
            {
                "job_id": deletion.job_id,
                "worker_id": worker_id,
                "reason_code": reason_code,
                "delay_seconds": delay_seconds,
            },
        )
