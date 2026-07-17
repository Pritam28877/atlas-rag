from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog.enums import VersionState
from app.services.ingestion.models import DocumentRoute
from app.services.ingestion.repository_support import (
    ClaimedNativeJob,
    NativeJobRepositorySupport,
    NextStageJob,
    PublishedArtifact,
)


class NativeJobRepository(NativeJobRepositorySupport):
    async def claim(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        document_version_id: UUID,
        job_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> ClaimedNativeJob | None:
        row_result = (
            await session.execute(
                text(
                    """
                    SELECT job.*, version.state AS version_state,
                           version.state_revision, version.progress_completed,
                           version.progress_total, version.object_key,
                           version.content_sha256, version.size_bytes,
                           version.pipeline_profile,
                           original.id AS original_artifact_id
                    FROM ingestion_jobs AS job
                    JOIN document_versions AS version
                      ON version.tenant_id = job.tenant_id
                     AND version.collection_id = job.collection_id
                     AND version.id = job.document_version_id
                    JOIN artifacts AS original
                      ON original.tenant_id = version.tenant_id
                     AND original.collection_id = version.collection_id
                     AND original.document_version_id = version.id
                     AND original.artifact_type = 'original_pdf'
                    WHERE job.id = :job_id AND job.tenant_id = :tenant_id
                      AND job.document_version_id = :version_id
                      AND job.stage IN ('preflight', 'native_parse')
                    FOR UPDATE OF job, version
                    """
                ),
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "version_id": document_version_id,
                },
            )
        ).mappings().one_or_none()
        if row_result is None:
            raise ValueError("native ingestion job identity is invalid")
        row: Mapping[str, object] = dict(row_result)
        if row["state"] in {"succeeded", "failed", "cancelled", "dead_lettered"}:
            return None
        if VersionState(str(row["version_state"])) in {
            VersionState.CANCELLED,
            VersionState.DELETED,
            VersionState.REJECTED,
            VersionState.FAILED,
            VersionState.QUARANTINED,
        }:
            await self._cancel_job(session, row, worker_id)
            return None
        lease_expires_at = row["lease_expires_at"]
        if row["state"] in {"leased", "running"} and isinstance(
            lease_expires_at, datetime
        ):
            database_now = await session.scalar(text("SELECT now()"))
            if isinstance(database_now, datetime) and lease_expires_at > database_now:
                return None
        attempt_count = int(str(row["attempt_count"]))
        max_attempts = int(str(row["max_attempts"]))
        if attempt_count >= max_attempts:
            await self._exhaust_job(session, row, worker_id)
            return None
        if row["state"] in {"leased", "running"}:
            await session.execute(
                text(
                    """
                    UPDATE job_attempts
                    SET finished_at = now(), heartbeat_at = now(),
                        outcome = 'lease_expired', error_class = 'internal',
                        reason_code = 'WORKER_LEASE_EXPIRED'
                    WHERE tenant_id = :tenant_id AND job_id = :job_id
                      AND attempt_number = :attempt_number
                      AND finished_at IS NULL
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "job_id": job_id,
                    "attempt_number": row["attempt_count"],
                },
            )
        attempt_number = attempt_count + 1
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET state = 'running', attempt_count = :attempt_number,
                    lease_owner = :worker_id,
                    lease_expires_at = now() + :lease_duration,
                    heartbeat_at = now(), retry_at = NULL, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {
                "attempt_number": attempt_number,
                "worker_id": worker_id,
                "lease_duration": lease_duration,
                "job_id": job_id,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO job_attempts (
                    id, tenant_id, collection_id, job_id, attempt_number,
                    worker_id, started_at, heartbeat_at
                ) VALUES (
                    :id, :tenant_id, :collection_id, :job_id, :attempt_number,
                    :worker_id, now(), now()
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "collection_id": row["collection_id"],
                "job_id": job_id,
                "attempt_number": attempt_number,
                "worker_id": worker_id,
            },
        )
        if row["version_state"] == VersionState.QUEUED:
            await self._transition(
                session,
                row,
                VersionState.PARSING,
                worker_id,
                f"worker:{job_id}:attempt:{attempt_number}:parsing",
            )
        return ClaimedNativeJob(
            id=job_id,
            tenant_id=tenant_id,
            collection_id=UUID(str(row["collection_id"])),
            document_version_id=document_version_id,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            object_key=str(row["object_key"]),
            content_sha256=str(row["content_sha256"]),
            size_bytes=int(str(row["size_bytes"])),
            pipeline_profile=str(row["pipeline_profile"]),
            original_artifact_id=UUID(str(row["original_artifact_id"])),
        )

    async def succeed(
        self,
        session: AsyncSession,
        job: ClaimedNativeJob,
        worker_id: str,
        route: DocumentRoute,
        page_count: int,
        artifacts: Sequence[PublishedArtifact],
        max_attempts: int,
    ) -> NextStageJob:
        locked = await self._locked_job_version(session, job, worker_id)
        for artifact in artifacts:
            await session.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        id, tenant_id, collection_id, document_version_id,
                        source_artifact_id, artifact_type, object_key,
                        generator_name, generator_version, checksum_sha256,
                        size_bytes
                    ) VALUES (
                        :id, :tenant_id, :collection_id, :version_id,
                        :source_artifact_id, :artifact_type, :object_key,
                        :generator_name, :generator_version, :checksum, :size_bytes
                    ) ON CONFLICT ON CONSTRAINT uq_artifacts_output DO NOTHING
                    """
                ),
                {
                    "id": uuid4(),
                    "tenant_id": job.tenant_id,
                    "collection_id": job.collection_id,
                    "version_id": job.document_version_id,
                    "source_artifact_id": job.original_artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "object_key": artifact.object_key,
                    "generator_name": artifact.generator_name,
                    "generator_version": artifact.generator_version,
                    "checksum": artifact.checksum_sha256,
                    "size_bytes": artifact.size_bytes,
                },
            )
        await session.execute(
            text(
                """
                UPDATE document_versions SET page_count = :page_count
                WHERE tenant_id = :tenant_id AND id = :version_id
                """
            ),
            {
                "page_count": page_count,
                "tenant_id": job.tenant_id,
                "version_id": job.document_version_id,
            },
        )
        version_row: Mapping[str, object] = locked
        if route is DocumentRoute.NATIVE:
            version_row = await self._transition(
                session,
                version_row,
                VersionState.NORMALIZING,
                worker_id,
                f"worker:{job.id}:normalizing",
            )
            target_state = VersionState.CHUNKING
            next_stage = "chunk"
        else:
            target_state = VersionState.OCR
            next_stage = "ocr"
        await self._transition(
            session,
            version_row,
            target_state,
            worker_id,
            f"worker:{job.id}:{target_state.value}",
        )
        next_job_id = await self._create_next_job(
            session,
            job,
            next_stage,
            max_attempts,
        )
        await self._finish_attempt(session, job, "succeeded", None, None)
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET state = 'succeeded', lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = now(), updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": job.id},
        )
        return NextStageJob(id=next_job_id, stage=next_stage)

    async def fail_permanently(
        self,
        session: AsyncSession,
        job: ClaimedNativeJob,
        worker_id: str,
        state: VersionState,
        reason_code: str,
        artifacts: Sequence[PublishedArtifact] = (),
    ) -> None:
        locked = await self._locked_job_version(session, job, worker_id)
        for artifact in artifacts:
            await self._insert_artifact(session, job, artifact)
        await self._transition(
            session,
            locked,
            state,
            worker_id,
            f"worker:{job.id}:terminal:{reason_code}",
            reason_code=reason_code,
        )
        await self._finish_attempt(
            session,
            job,
            "permanent_failure",
            "invalid_input" if state is not VersionState.QUARANTINED else "security",
            reason_code,
        )
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET state = 'failed', terminal_reason_code = :reason_code,
                    lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = now(), updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": job.id, "reason_code": reason_code},
        )
        await self._finish_lifecycle_request(session, job.id, "failed")

    async def _insert_artifact(
        self,
        session: AsyncSession,
        job: ClaimedNativeJob,
        artifact: PublishedArtifact,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, tenant_id, collection_id, document_version_id,
                    source_artifact_id, artifact_type, object_key,
                    generator_name, generator_version, checksum_sha256,
                    size_bytes
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :source_artifact_id, :artifact_type, :object_key,
                    :generator_name, :generator_version, :checksum, :size_bytes
                ) ON CONFLICT ON CONSTRAINT uq_artifacts_output DO NOTHING
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
                "source_artifact_id": job.original_artifact_id,
                "artifact_type": artifact.artifact_type,
                "object_key": artifact.object_key,
                "generator_name": artifact.generator_name,
                "generator_version": artifact.generator_version,
                "checksum": artifact.checksum_sha256,
                "size_bytes": artifact.size_bytes,
            },
        )

    async def schedule_retry(
        self,
        session: AsyncSession,
        job: ClaimedNativeJob,
        worker_id: str,
        reason_code: str,
        delay_seconds: int,
    ) -> None:
        await self._locked_job_version(session, job, worker_id)
        await self._finish_attempt(
            session,
            job,
            "retryable_failure",
            "dependency",
            reason_code,
        )
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET state = 'retry_scheduled', retry_at = now()
                    + (:delay_seconds * interval '1 second'),
                    terminal_reason_code = :reason_code,
                    lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = now(), updated_at = now()
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job.id,
                "reason_code": reason_code,
                "delay_seconds": delay_seconds,
            },
        )

    async def _finish_lifecycle_request(
        self, session: AsyncSession, job_id: UUID, state: str
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE lifecycle_requests request
                SET state = :state, completed_at = now()
                FROM ingestion_jobs job
                WHERE job.id = :job_id
                  AND job.lifecycle_request_id = request.id
                  AND request.state IN ('pending', 'running')
                """
            ),
            {"job_id": job_id, "state": state},
        )
