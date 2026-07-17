from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog.enums import VersionState
from app.services.catalog.state_machine import (
    PublicationEvidence,
    VersionSnapshot,
    transition_version,
)
from app.services.ingestion.errors import JobFenceLostError


@dataclass(frozen=True, slots=True)
class ClaimedNativeJob:
    id: UUID
    tenant_id: UUID
    collection_id: UUID
    document_version_id: UUID
    attempt_number: int
    max_attempts: int
    object_key: str
    content_sha256: str
    size_bytes: int
    pipeline_profile: str
    original_artifact_id: UUID


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    artifact_type: str
    object_key: str
    generator_name: str
    generator_version: str
    checksum_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class NextStageJob:
    id: UUID
    stage: str


async def defer_job_dispatch(
    session: AsyncSession,
    tenant_id: UUID,
    document_version_id: UUID,
    job: NextStageJob,
    reason_code: str,
    retry_delay: timedelta,
) -> None:
    """Persist a bounded retry for a committed successor, never its predecessor."""
    await session.execute(
        text(
            """
            UPDATE ingestion_jobs
            SET state = 'retry_scheduled', retry_at = now() + :retry_delay,
                updated_at = now(),
                sanitized_context = COALESCE(sanitized_context, '{}'::jsonb)
                    || jsonb_build_object(
                        'dispatch_failure_reason', CAST(:reason_code AS text)
                    )
            WHERE id = :job_id AND tenant_id = :tenant_id
              AND document_version_id = :version_id
              AND state IN ('pending', 'retry_scheduled')
            """
        ),
        {
            "retry_delay": retry_delay,
            "reason_code": reason_code,
            "job_id": job.id,
            "tenant_id": tenant_id,
            "version_id": document_version_id,
        },
    )


class NativeJobRepositorySupport:
    async def pending_successor(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        document_version_id: UUID,
        predecessor_job_id: UUID,
    ) -> NextStageJob | None:
        """Return only the immediate, unclaimed successor of a succeeded job."""
        row = (
            await session.execute(
                text(
                    """
                    SELECT successor.id, successor.stage
                    FROM ingestion_jobs predecessor
                    JOIN ingestion_jobs successor
                      ON successor.tenant_id = predecessor.tenant_id
                     AND successor.collection_id = predecessor.collection_id
                     AND successor.document_version_id =
                         predecessor.document_version_id
                     AND successor.lifecycle_request_id IS NOT DISTINCT FROM
                         predecessor.lifecycle_request_id
                    WHERE predecessor.id = :predecessor_job_id
                      AND predecessor.tenant_id = :tenant_id
                      AND predecessor.document_version_id = :version_id
                      AND predecessor.state = 'succeeded'
                      AND successor.state IN ('pending', 'retry_scheduled')
                      AND (
                        (predecessor.stage IN ('preflight', 'native_parse')
                          AND successor.stage IN ('ocr', 'chunk'))
                        OR (predecessor.stage = 'ocr' AND successor.stage = 'chunk')
                        OR (predecessor.stage = 'chunk' AND successor.stage = 'embed')
                        OR (predecessor.stage = 'embed' AND successor.stage = 'index')
                      )
                    ORDER BY successor.created_at, successor.id
                    LIMIT 1
                    """
                ),
                {
                    "predecessor_job_id": predecessor_job_id,
                    "tenant_id": tenant_id,
                    "version_id": document_version_id,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return NextStageJob(id=UUID(str(row["id"])), stage=str(row["stage"]))

    async def _locked_job_version(
        self, session: AsyncSession, job: ClaimedNativeJob, worker_id: str
    ) -> Mapping[str, object]:
        row = (
            await session.execute(
                text(
                    """
                    SELECT version.*, ingestion.state AS job_state,
                           ingestion.lease_owner AS job_lease_owner,
                           ingestion.attempt_count AS job_attempt_count
                    FROM document_versions AS version
                    JOIN ingestion_jobs AS ingestion
                      ON ingestion.tenant_id = version.tenant_id
                     AND ingestion.collection_id = version.collection_id
                     AND ingestion.document_version_id = version.id
                    WHERE version.tenant_id = :tenant_id
                      AND version.id = :version_id AND ingestion.id = :job_id
                    FOR UPDATE OF version, ingestion
                    """
                ),
                {
                    "tenant_id": job.tenant_id,
                    "version_id": job.document_version_id,
                    "job_id": job.id,
                },
            )
        ).mappings().one()
        locked = dict(row)
        owns_attempt = (
            locked["job_state"] == "running"
            and locked["job_lease_owner"] == worker_id
            and int(str(locked["job_attempt_count"])) == job.attempt_number
        )
        if not owns_attempt:
            raise JobFenceLostError("durable job ownership was revoked")
        return locked

    async def _transition(
        self,
        session: AsyncSession,
        row: Mapping[str, object],
        target: VersionState,
        worker_id: str,
        event_key: str,
        reason_code: str | None = None,
        publication_evidence: PublicationEvidence | None = None,
    ) -> Mapping[str, object]:
        snapshot = VersionSnapshot(
            state=VersionState(str(row.get("version_state", row["state"]))),
            revision=int(str(row["state_revision"])),
            progress_completed=int(str(row["progress_completed"])),
            progress_total=int(str(row["progress_total"])),
        )
        transitioned = transition_version(
            snapshot,
            target,
            expected_revision=snapshot.revision,
            terminal_reason_code=reason_code,
            publication_evidence=publication_evidence,
        )
        version_id = row.get("document_version_id", row["id"])
        updated = (
            await session.execute(
                text(
                    """
                    UPDATE document_versions
                    SET state = :state, state_revision = :revision,
                        terminal_reason_code = :reason_code, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :version_id
                      AND state_revision = :expected_revision
                    RETURNING *
                    """
                ),
                {
                    "state": transitioned.state,
                    "revision": transitioned.revision,
                    "reason_code": reason_code,
                    "tenant_id": row["tenant_id"],
                    "version_id": version_id,
                    "expected_revision": snapshot.revision,
                },
            )
        ).mappings().one()
        await session.execute(
            text(
                """
                INSERT INTO version_transition_events (
                    id, tenant_id, collection_id, document_version_id,
                    idempotency_key, from_state, to_state, from_revision,
                    to_revision, operation, actor_type, actor_id
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :event_key, :from_state, :to_state, :from_revision,
                    :to_revision, 'advance', 'worker', :worker_id
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": row["tenant_id"],
                "collection_id": row["collection_id"],
                "version_id": version_id,
                "event_key": event_key,
                "from_state": snapshot.state,
                "to_state": target,
                "from_revision": snapshot.revision,
                "to_revision": transitioned.revision,
                "worker_id": worker_id,
            },
        )
        return dict(updated)

    async def _create_next_job(
        self,
        session: AsyncSession,
        job: ClaimedNativeJob,
        stage: str,
        max_attempts: int,
    ) -> UUID:
        next_job_id = uuid4()
        returned_id = await session.scalar(
            text(
                """
                INSERT INTO ingestion_jobs (
                    id, tenant_id, collection_id, document_version_id,
                    stage, generation, max_attempts, lifecycle_request_id
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :stage, 0, :max_attempts,
                    (SELECT lifecycle_request_id FROM ingestion_jobs
                     WHERE id = :current_job_id)
                ) ON CONFLICT ON CONSTRAINT uq_ingestion_jobs_delivery
                DO UPDATE SET updated_at = ingestion_jobs.updated_at
                RETURNING id
                """
            ),
            {
                "id": next_job_id,
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
                "stage": stage,
                "max_attempts": max_attempts,
                "current_job_id": job.id,
            },
        )
        return returned_id

    async def _finish_attempt(
        self,
        session: AsyncSession,
        job: ClaimedNativeJob,
        outcome: str,
        error_class: str | None,
        reason_code: str | None,
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE job_attempts
                SET finished_at = now(), heartbeat_at = now(), outcome = :outcome,
                    error_class = :error_class, reason_code = :reason_code
                WHERE tenant_id = :tenant_id AND job_id = :job_id
                  AND attempt_number = :attempt_number
                  AND finished_at IS NULL
                """
            ),
            {
                "outcome": outcome,
                "error_class": error_class,
                "reason_code": reason_code,
                "tenant_id": job.tenant_id,
                "job_id": job.id,
                "attempt_number": job.attempt_number,
            },
        )

    async def _cancel_job(
        self, session: AsyncSession, row: Mapping[str, object], worker_id: str
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET state = 'cancelled', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": row["id"]},
        )
        await session.execute(
            text(
                """
                UPDATE job_attempts SET finished_at = now(), heartbeat_at = now(),
                    outcome = 'cancelled', reason_code = 'LIFECYCLE_CANCELLED'
                WHERE tenant_id = :tenant_id AND job_id = :job_id
                  AND finished_at IS NULL
                """
            ),
            {"tenant_id": row["tenant_id"], "job_id": row["id"]},
        )

    async def _exhaust_job(
        self, session: AsyncSession, row: Mapping[str, object], worker_id: str
    ) -> None:
        await self._transition(
            session,
            row,
            VersionState.FAILED,
            worker_id,
            f"worker:{row['id']}:attempts-exhausted",
            reason_code="PROCESSING_RETRY_EXHAUSTED",
        )
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET state = 'dead_lettered',
                    terminal_reason_code = 'PROCESSING_RETRY_EXHAUSTED',
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": row["id"]},
        )
