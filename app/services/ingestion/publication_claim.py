"""Claim and lease handling for durable publication jobs."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog.enums import VersionState
from app.services.ingestion.publication_models import ClaimedPublicationJob
from app.services.ingestion.repository import NativeJobRepository
from app.services.ingestion.repository_support import ClaimedNativeJob


class PublicationClaimRepository(NativeJobRepository):
    async def claim(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        document_version_id: UUID,
        job_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> ClaimedPublicationJob | None:
        row_result = (
            await session.execute(
                text(
                    """
                    SELECT job.*, version.state AS version_state,
                           version.state_revision, version.progress_completed,
                           version.progress_total, version.object_key,
                           version.content_sha256, version.size_bytes,
                           version.pipeline_profile,
                           COALESCE(version.reprocessed_from_version_id, version.id)
                               AS source_document_version_id,
                           normalized_source.id
                               AS normalized_document_version_id,
                           original.id AS original_artifact_id,
                           normalized.id AS normalized_artifact_id,
                           normalized.object_key AS normalized_object_key,
                           normalized.checksum_sha256 AS normalized_checksum,
                           normalized.generator_version AS normalized_version,
                           chunk.object_key AS chunk_object_key,
                           chunk.checksum_sha256 AS chunk_checksum,
                           chunk.generator_version AS chunk_version,
                           embedding.object_key AS embedding_object_key,
                           embedding.checksum_sha256 AS embedding_checksum,
                           embedding.generator_version AS embedding_version
                    FROM ingestion_jobs AS job
                    JOIN document_versions AS version
                      ON version.tenant_id = job.tenant_id
                     AND version.collection_id = job.collection_id
                     AND version.id = job.document_version_id
                    JOIN LATERAL (
                        WITH RECURSIVE lineage AS (
                            SELECT candidate.id,
                                   candidate.reprocessed_from_version_id,
                                   0 AS depth
                            FROM document_versions candidate
                            WHERE candidate.tenant_id = version.tenant_id
                              AND candidate.collection_id = version.collection_id
                              AND candidate.id = version.id
                            UNION ALL
                            SELECT parent.id,
                                   parent.reprocessed_from_version_id,
                                   lineage.depth + 1
                            FROM document_versions parent
                            JOIN lineage
                              ON parent.id = lineage.reprocessed_from_version_id
                            WHERE parent.tenant_id = version.tenant_id
                              AND parent.collection_id = version.collection_id
                              AND lineage.depth < 100
                        )
                        SELECT lineage.id
                        FROM lineage
                        WHERE EXISTS (
                            SELECT 1 FROM artifacts candidate
                            WHERE candidate.tenant_id = version.tenant_id
                              AND candidate.collection_id = version.collection_id
                              AND candidate.document_version_id = lineage.id
                              AND candidate.artifact_type = 'normalized_document'
                        )
                        ORDER BY lineage.depth LIMIT 1
                    ) normalized_source ON TRUE
                    JOIN artifacts AS original
                      ON original.tenant_id = version.tenant_id
                     AND original.collection_id = version.collection_id
                     AND original.document_version_id = normalized_source.id
                     AND original.artifact_type = 'original_pdf'
                    JOIN LATERAL (
                        SELECT * FROM artifacts candidate
                        WHERE candidate.tenant_id = version.tenant_id
                          AND candidate.collection_id = version.collection_id
                          AND candidate.document_version_id = normalized_source.id
                          AND candidate.artifact_type = 'normalized_document'
                        ORDER BY candidate.created_at DESC, candidate.id DESC LIMIT 1
                    ) normalized ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT * FROM artifacts candidate
                        WHERE candidate.tenant_id = version.tenant_id
                          AND candidate.collection_id = version.collection_id
                          AND candidate.document_version_id = version.id
                          AND candidate.artifact_type = 'chunk_manifest'
                        ORDER BY candidate.created_at DESC, candidate.id DESC LIMIT 1
                    ) chunk ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT * FROM artifacts candidate
                        WHERE candidate.tenant_id = version.tenant_id
                          AND candidate.collection_id = version.collection_id
                          AND candidate.document_version_id = version.id
                          AND candidate.artifact_type = 'index_manifest'
                          AND candidate.generator_name = 'fastembed'
                        ORDER BY candidate.created_at DESC, candidate.id DESC LIMIT 1
                    ) embedding ON TRUE
                    WHERE job.id = :job_id AND job.tenant_id = :tenant_id
                      AND job.document_version_id = :version_id
                      AND job.stage IN ('chunk', 'embed', 'index')
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
            raise ValueError("publication job identity is invalid")
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
        expected_state = {
            "chunk": VersionState.CHUNKING,
            "embed": VersionState.EMBEDDING,
            "index": VersionState.INDEXING,
        }[str(row["stage"])]
        if VersionState(str(row["version_state"])) is not expected_state:
            raise ValueError("document version is not awaiting this publication stage")
        if row["stage"] in {"embed", "index"} and row["chunk_object_key"] is None:
            raise ValueError("publication job is missing its chunk manifest")
        if row["stage"] == "index" and row["embedding_object_key"] is None:
            raise ValueError("index job is missing its embedding manifest")
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
            await self._expire_attempt(session, row, tenant_id, job_id)
        attempt_number = attempt_count + 1
        await self._start_attempt(
            session, row, job_id, tenant_id, worker_id, attempt_number, lease_duration
        )
        return _claimed_job(row, tenant_id, document_version_id, job_id, attempt_number)

    async def _expire_attempt(
        self,
        session: AsyncSession,
        row: Mapping[str, object],
        tenant_id: UUID,
        job_id: UUID,
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE job_attempts SET finished_at = now(), heartbeat_at = now(),
                    outcome = 'lease_expired', error_class = 'internal',
                    reason_code = 'WORKER_LEASE_EXPIRED'
                WHERE tenant_id = :tenant_id AND job_id = :job_id
                  AND attempt_number = :attempt_number AND finished_at IS NULL
                """
            ),
            {
                "tenant_id": tenant_id,
                "job_id": job_id,
                "attempt_number": row["attempt_count"],
            },
        )

    async def _start_attempt(
        self,
        session: AsyncSession,
        row: Mapping[str, object],
        job_id: UUID,
        tenant_id: UUID,
        worker_id: str,
        attempt_number: int,
        lease_duration: timedelta,
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs SET state = 'running',
                    attempt_count = :attempt_number, lease_owner = :worker_id,
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

    async def _complete_job(
        self, session: AsyncSession, job: ClaimedNativeJob
    ) -> None:
        await self._finish_attempt(session, job, "succeeded", None, None)
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs SET state = 'succeeded', lease_owner = NULL,
                    lease_expires_at = NULL, heartbeat_at = now(), updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": job.id},
        )


def _claimed_job(
    row: Mapping[str, object],
    tenant_id: UUID,
    document_version_id: UUID,
    job_id: UUID,
    attempt_number: int,
) -> ClaimedPublicationJob:
    optional_values = {
        name: str(row[name]) if row[name] is not None else None
        for name in (
            "chunk_object_key",
            "chunk_checksum",
            "chunk_version",
            "embedding_object_key",
            "embedding_checksum",
            "embedding_version",
        )
    }
    return ClaimedPublicationJob(
        id=job_id,
        tenant_id=tenant_id,
        collection_id=UUID(str(row["collection_id"])),
        document_version_id=document_version_id,
        attempt_number=attempt_number,
        max_attempts=int(str(row["max_attempts"])),
        object_key=str(row["object_key"]),
        content_sha256=str(row["content_sha256"]),
        size_bytes=int(str(row["size_bytes"])),
        pipeline_profile=str(row["pipeline_profile"]),
        original_artifact_id=UUID(str(row["original_artifact_id"])),
        stage=str(row["stage"]),
        source_document_version_id=UUID(
            str(row["source_document_version_id"])
        ),
        normalized_document_version_id=UUID(
            str(row["normalized_document_version_id"])
        ),
        normalized_artifact_id=UUID(str(row["normalized_artifact_id"])),
        normalized_object_key=str(row["normalized_object_key"]),
        normalized_checksum_sha256=str(row["normalized_checksum"]),
        normalized_generator_version=str(row["normalized_version"]),
        chunk_object_key=optional_values["chunk_object_key"],
        chunk_checksum_sha256=optional_values["chunk_checksum"],
        chunk_generator_version=optional_values["chunk_version"],
        embedding_object_key=optional_values["embedding_object_key"],
        embedding_checksum_sha256=optional_values["embedding_checksum"],
        embedding_generator_version=optional_values["embedding_version"],
    )
