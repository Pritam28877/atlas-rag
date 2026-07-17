"""SQL helpers for lease-fenced, bounded physical deletion."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ingestion.errors import JobFenceLostError
from app.services.ingestion.publication_models import SearchTarget
from app.services.lifecycle.deletion_models import ClaimedDeletion
from app.services.lifecycle.deletion_sql import (
    ELIGIBLE_PENDING_COUNT_SQL,
    INITIALIZE_CLEANUP_LEDGER_SQL,
    PENDING_CLEANUP_KEYS_SQL,
    RECHECK_CLEANUP_KEYS_SQL,
    SEARCH_TARGETS_SQL,
)


class DeletionRepositorySupport:
    async def _search_targets(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        document_version_id: UUID,
    ) -> tuple[SearchTarget, ...]:
        rows = (
            await session.execute(
                text(SEARCH_TARGETS_SQL),
                {"tenant_id": tenant_id, "version_id": document_version_id},
            )
        ).mappings().all()
        return tuple(
            SearchTarget(
                name=str(row["target_name"]),
                version=str(row["target_version"]),
            )
            for row in rows
        )

    async def _active_lease(self, session: AsyncSession, row) -> bool:
        lease_expires_at = row["lease_expires_at"]
        if row["state"] not in {"leased", "running"} or not isinstance(
            lease_expires_at, datetime
        ):
            return False
        database_now = await session.scalar(text("SELECT now()"))
        if isinstance(database_now, datetime) and lease_expires_at > database_now:
            return True
        await session.execute(
            text(
                """
                UPDATE job_attempts SET finished_at = now(), heartbeat_at = now(),
                    outcome = 'lease_expired', error_class = 'internal',
                    reason_code = 'WORKER_LEASE_EXPIRED'
                WHERE tenant_id = :tenant_id AND job_id = :job_id
                  AND attempt_number = :attempt AND finished_at IS NULL
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "job_id": row["id"],
                "attempt": row["attempt_count"],
            },
        )
        return False

    async def _start_attempt(
        self, session: AsyncSession, row, worker_id: str, attempt_number: int
    ) -> None:
        attempt_parameters = {
            "attempt": attempt_number,
            "worker_id": worker_id,
            "job_id": row["id"],
            "request_id": row["request_id"],
            "attempt_id": uuid4(),
            "tenant_id": row["tenant_id"],
            "collection_id": row["collection_id"],
        }
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs SET state = 'running',
                    attempt_count = :attempt, lease_owner = :worker_id,
                    lease_expires_at = now() + interval '5 minutes',
                    heartbeat_at = now(), retry_at = NULL, updated_at = now()
                WHERE id = :job_id
                """
            ),
            attempt_parameters,
        )
        await session.execute(
            text(
                """
                UPDATE lifecycle_requests SET state = 'running'
                WHERE id = :request_id AND state = 'pending'
                """
            ),
            attempt_parameters,
        )
        await session.execute(
            text(
                """
                INSERT INTO job_attempts (
                    id, tenant_id, collection_id, job_id, attempt_number,
                    worker_id, started_at, heartbeat_at
                ) VALUES (
                    :attempt_id, :tenant_id, :collection_id, :job_id,
                    :attempt, :worker_id, now(), now()
                )
                """
            ),
            attempt_parameters,
        )

    async def _cleanup_due(self, session: AsyncSession, row) -> bool:
        has_surviving_descendant = await session.scalar(
            text(
                """
                WITH RECURSIVE descendants AS (
                    SELECT child.id
                    FROM document_versions child
                    WHERE child.tenant_id = :tenant_id
                      AND child.collection_id = :collection_id
                      AND child.reprocessed_from_version_id = :version_id
                    UNION
                    SELECT child.id
                    FROM document_versions child
                    JOIN descendants parent
                      ON child.reprocessed_from_version_id = parent.id
                    WHERE child.tenant_id = :tenant_id
                      AND child.collection_id = :collection_id
                )
                SELECT EXISTS (
                    SELECT 1 FROM document_versions version
                    JOIN descendants ON descendants.id = version.id
                    WHERE version.state <> 'deleted'
                )
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "collection_id": row["collection_id"],
                "version_id": row["document_version_id"],
            },
        )
        if has_surviving_descendant:
            return False
        return not bool(row["legal_hold"]) and bool(
            await session.scalar(
                text(
                    """
                    SELECT now() >= :created_at
                        + (:retention_days * interval '1 day')
                    """
                ),
                {
                    "created_at": row["created_at"],
                    "retention_days": row["retention_days"],
                },
            )
        )

    async def _initialize_cleanup_ledger(self, session: AsyncSession, row) -> None:
        await session.execute(
            text(INITIALIZE_CLEANUP_LEDGER_SQL),
            {
                "tenant_id": row["tenant_id"],
                "collection_id": row["collection_id"],
                "version_id": row["document_version_id"],
                "request_id": row["request_id"],
                "original_key": row["object_key"],
            },
        )

    async def _pending_keys_for_deletion(
        self, session: AsyncSession, deletion: ClaimedDeletion, limit: int
    ) -> tuple[str, ...]:
        candidates: Sequence[str] = (
            await session.scalars(
                text(PENDING_CLEANUP_KEYS_SQL),
                {
                    "tenant_id": deletion.tenant_id,
                    "version_id": deletion.document_version_id,
                    "limit": limit,
                },
            )
        ).all()
        candidate_keys = tuple(str(value) for value in candidates)
        if not candidate_keys:
            return ()
        await session.execute(
            text(
                """
                SELECT id FROM artifacts
                WHERE tenant_id = :tenant_id
                  AND document_version_id = :version_id
                  AND object_key = ANY(:object_keys)
                FOR UPDATE
                """
            ),
            {
                "tenant_id": deletion.tenant_id,
                "version_id": deletion.document_version_id,
                "object_keys": list(candidate_keys),
            },
        )
        eligible: Sequence[str] = (
            await session.scalars(
                text(RECHECK_CLEANUP_KEYS_SQL),
                {
                    "tenant_id": deletion.tenant_id,
                    "version_id": deletion.document_version_id,
                    "object_keys": list(candidate_keys),
                },
            )
        ).all()
        return tuple(str(value) for value in eligible)

    async def _eligible_pending_count(
        self, session: AsyncSession, deletion: ClaimedDeletion
    ) -> int:
        value = await session.scalar(
            text(ELIGIBLE_PENDING_COUNT_SQL),
            {
                "tenant_id": deletion.tenant_id,
                "version_id": deletion.document_version_id,
            },
        )
        return int(str(value))

    async def _lock_fence(self, session, deletion, worker_id: str):
        row = (
            await session.execute(
                text(
                    """
                    SELECT job.id, job.tenant_id, job.collection_id,
                           job.document_version_id,
                           job.lifecycle_request_id AS request_id,
                           version.state, version.state_revision,
                           version.object_key, version.created_at,
                           collection.legal_hold, collection.retention_days
                    FROM document_versions version
                    JOIN ingestion_jobs job
                      ON job.tenant_id = version.tenant_id
                     AND job.document_version_id = version.id
                    JOIN collections collection
                      ON collection.tenant_id = version.tenant_id
                     AND collection.id = version.collection_id
                    WHERE job.id = :job_id AND job.tenant_id = :tenant_id
                      AND job.state = 'running' AND job.lease_owner = :worker_id
                      AND job.attempt_count = :attempt
                    FOR UPDATE OF collection, version, job
                    """
                ),
                {
                    "job_id": deletion.job_id,
                    "tenant_id": deletion.tenant_id,
                    "worker_id": worker_id,
                    "attempt": deletion.attempt_number,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            raise JobFenceLostError("deletion job ownership was revoked")
        return row

    async def _mark_search_revoked(self, session, deletion) -> None:
        await session.execute(
            text(
                """
                UPDATE index_publications SET deleted_at = COALESCE(deleted_at, now())
                WHERE tenant_id = :tenant_id AND document_version_id = :version_id
                """
            ),
            {
                "tenant_id": deletion.tenant_id,
                "version_id": deletion.document_version_id,
            },
        )

    async def _mark_version_deleted(
        self, session, deletion, worker_id: str, version
    ) -> None:
        state_key = "version_state" if "version_state" in version else "state"
        version_state = str(version[state_key])
        if version_state == "deleted":
            return
        revision = int(str(version["state_revision"]))
        transition_parameters = {
            "event_id": uuid4(),
            "tenant_id": deletion.tenant_id,
            "collection_id": deletion.collection_id,
            "version_id": deletion.document_version_id,
            "event_key": f"lifecycle:{deletion.request_id}:delete",
            "from_state": version_state,
            "revision": revision,
            "next_revision": revision + 1,
            "worker_id": worker_id,
        }
        await session.execute(
            text(
                """
                UPDATE document_versions SET state = 'deleted',
                    state_revision = :next_revision, updated_at = now()
                WHERE tenant_id = :tenant_id AND id = :version_id
                  AND state_revision = :revision
                """
            ),
            transition_parameters,
        )
        await session.execute(
            text(
                """
                INSERT INTO version_transition_events (
                    id, tenant_id, collection_id, document_version_id,
                    idempotency_key, from_state, to_state, from_revision,
                    to_revision, operation, actor_type, actor_id
                ) VALUES (
                    :event_id, :tenant_id, :collection_id, :version_id,
                    :event_key, :from_state, 'deleted', :revision,
                    :next_revision, 'delete', 'worker', :worker_id
                ) ON CONFLICT ON CONSTRAINT uq_version_transition_idempotency
                  DO NOTHING
                """
            ),
            transition_parameters,
        )

    async def _finish_attempt(
        self, session, deletion, outcome: str, reason_code: str | None
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE job_attempts SET finished_at = now(), heartbeat_at = now(),
                    outcome = :outcome,
                    error_class = CASE WHEN CAST(:reason_code AS text) IS NULL
                        THEN NULL ELSE 'dependency' END,
                    reason_code = :reason_code
                WHERE tenant_id = :tenant_id AND job_id = :job_id
                  AND attempt_number = :attempt AND finished_at IS NULL
                """
            ),
            {
                "outcome": outcome,
                "reason_code": reason_code,
                "tenant_id": deletion.tenant_id,
                "job_id": deletion.job_id,
                "attempt": deletion.attempt_number,
            },
        )

    async def _complete_request(self, session, request_id: UUID) -> None:
        await session.execute(
            text(
                """
                UPDATE lifecycle_requests SET state = 'succeeded', completed_at = now()
                WHERE id = :request_id AND state <> 'succeeded'
                """
            ),
            {"request_id": request_id},
        )

    async def _fail(self, session: AsyncSession, row) -> None:
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs SET state = 'dead_lettered',
                    terminal_reason_code = 'DELETE_RETRY_EXHAUSTED',
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": row["id"]},
        )
        await session.execute(
            text(
                """
                UPDATE lifecycle_requests SET state = 'failed', completed_at = now()
                WHERE id = :request_id AND state <> 'succeeded'
                """
            ),
            {"request_id": row["request_id"]},
        )
