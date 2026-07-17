from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog.enums import VersionState
from app.services.ingestion.repository import NativeJobRepository
from app.services.ingestion.repository_support import (
    ClaimedNativeJob,
    NextStageJob,
    PublishedArtifact,
)


@dataclass(frozen=True, slots=True)
class ClaimedOcrJob(ClaimedNativeJob):
    extracted_object_key: str
    extracted_checksum_sha256: str
    extracted_generator_version: str
    page_count: int


class OcrJobRepository(NativeJobRepository):
    async def claim(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        document_version_id: UUID,
        job_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> ClaimedOcrJob | None:
        row_result = (
            await session.execute(
                text(
                    """
                    SELECT job.*, version.state AS version_state,
                           version.state_revision, version.progress_completed,
                           version.progress_total, version.object_key,
                           version.content_sha256, version.size_bytes,
                           version.pipeline_profile, version.page_count,
                           original.id AS original_artifact_id,
                           extracted.object_key AS extracted_object_key,
                           extracted.checksum_sha256 AS extracted_checksum,
                           extracted.generator_version AS extracted_generator_version
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
                    JOIN artifacts AS extracted
                      ON extracted.tenant_id = version.tenant_id
                     AND extracted.collection_id = version.collection_id
                     AND extracted.document_version_id = version.id
                     AND extracted.artifact_type = 'extracted_pages'
                    WHERE job.id = :job_id AND job.tenant_id = :tenant_id
                      AND job.document_version_id = :version_id
                      AND job.stage = 'ocr'
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
            raise ValueError("OCR ingestion job identity is invalid")
        row: Mapping[str, object] = dict(row_result)
        if row["state"] in {"succeeded", "failed", "cancelled", "dead_lettered"}:
            return None
        if VersionState(str(row["version_state"])) in {
            VersionState.CANCELLED,
            VersionState.DELETED,
            VersionState.FAILED,
        }:
            await self._cancel_job(session, row, worker_id)
            return None
        if row["version_state"] != VersionState.OCR:
            raise ValueError("document version is not awaiting OCR")
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
                    UPDATE job_attempts SET finished_at = now(),
                        heartbeat_at = now(), outcome = 'lease_expired',
                        error_class = 'internal',
                        reason_code = 'WORKER_LEASE_EXPIRED'
                    WHERE tenant_id = :tenant_id AND job_id = :job_id
                      AND attempt_number = :attempt_number
                      AND finished_at IS NULL
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "job_id": job_id,
                    "attempt_number": attempt_count,
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
        return ClaimedOcrJob(
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
            extracted_object_key=str(row["extracted_object_key"]),
            extracted_checksum_sha256=str(row["extracted_checksum"]),
            extracted_generator_version=str(row["extracted_generator_version"]),
            page_count=int(str(row["page_count"])),
        )

    async def succeed_ocr(
        self,
        session: AsyncSession,
        job: ClaimedOcrJob,
        worker_id: str,
        artifacts: Sequence[PublishedArtifact],
        max_attempts: int,
    ) -> NextStageJob:
        locked = await self._locked_job_version(session, job, worker_id)
        for artifact in artifacts:
            await self._insert_artifact(session, job, artifact)
        normalized = await self._transition(
            session,
            locked,
            VersionState.NORMALIZING,
            worker_id,
            f"worker:{job.id}:ocr-normalizing",
        )
        await self._transition(
            session,
            normalized,
            VersionState.CHUNKING,
            worker_id,
            f"worker:{job.id}:ocr-chunking",
        )
        chunk_job_id = await self._create_next_job(
            session,
            job,
            "chunk",
            max_attempts,
        )
        await self._finish_attempt(session, job, "succeeded", None, None)
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs SET state = 'succeeded',
                    lease_owner = NULL, lease_expires_at = NULL,
                    heartbeat_at = now(), updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": job.id},
        )
        return NextStageJob(id=chunk_job_id, stage="chunk")
