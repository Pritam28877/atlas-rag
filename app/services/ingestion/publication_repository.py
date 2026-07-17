"""Catalog writes and readiness reconciliation for publication jobs."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog.enums import VersionState
from app.services.catalog.state_machine import PublicationEvidence
from app.services.ingestion.publication_artifact_repository import (
    PublicationArtifactRepository,
)
from app.services.ingestion.publication_evidence import require_publication_evidence
from app.services.ingestion.publication_models import (
    ClaimedPublicationJob,
    PublicationRecord,
    SearchTarget,
    SearchTargetPublication,
)
from app.services.ingestion.publication_promotion import supersede_source_version
from app.services.ingestion.repository_support import PublishedArtifact
from app.services.ingestion.search_recovery import SearchRecoveryRepository


@dataclass(frozen=True, slots=True)
class PreparedIndexPublication:
    locked_version: dict[str, Any]
    evidence: PublicationEvidence
    source_targets: tuple[SearchTargetPublication, ...]


class PublicationJobRepository(PublicationArtifactRepository):
    async def index_cleanup_complete(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
    ) -> bool:
        unfinished = await session.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM search_cleanup_entries
                    WHERE tenant_id = :tenant_id
                      AND collection_id = :collection_id
                      AND activation_version_id = :version_id
                      AND state <> 'deleted'
                )
                """
            ),
            {
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
            },
        )
        return not bool(unfinished)

    async def begin_index_attempt(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        worker_id: str,
        target: SearchTarget,
    ) -> None:
        """Persist the exact external write fence before publishing any records."""
        await self._locked_job_version(session, job, worker_id)
        prior_job_target = (
            await session.execute(
                text(
                    """
                    SELECT publication_target_name, publication_target_version,
                           publication_target_attempt
                    FROM ingestion_jobs
                    WHERE id = :job_id
                    """
                ),
                {"job_id": job.id},
            )
        ).mappings().one()
        rows = (
            await session.execute(
                text(
                    """
                    SELECT target_name, target_version, publication_job_id,
                           publication_attempt,
                           count(DISTINCT chunk_id) AS record_count
                    FROM index_publications
                    WHERE tenant_id = :tenant_id
                      AND collection_id = :collection_id
                      AND document_version_id = :version_id
                      AND deleted_at IS NULL
                      AND (publication_job_id, publication_attempt)
                          IS DISTINCT FROM (:job_id, :attempt_number)
                    GROUP BY target_name, target_version,
                             publication_job_id, publication_attempt
                    """
                ),
                {
                    "tenant_id": job.tenant_id,
                    "collection_id": job.collection_id,
                    "version_id": job.document_version_id,
                    "job_id": job.id,
                    "attempt_number": job.attempt_number,
                },
            )
        ).mappings().all()
        retired: dict[
            tuple[str, str, UUID | None, int | None], SearchTargetPublication
        ] = {}
        for row in rows:
            publication_job_id = row["publication_job_id"]
            publication_attempt = row["publication_attempt"]
            key = (
                str(row["target_name"]),
                str(row["target_version"]),
                UUID(str(publication_job_id)) if publication_job_id else None,
                int(str(publication_attempt)) if publication_attempt else None,
            )
            retired[key] = SearchTargetPublication(
                target=SearchTarget(key[0], key[1]),
                record_count=max(1, int(str(row["record_count"]))),
                publication_job_id=key[2],
                publication_attempt=key[3],
            )
        prior_target_name = prior_job_target["publication_target_name"]
        prior_attempt = prior_job_target["publication_target_attempt"]
        if (
            prior_target_name is not None
            and int(str(prior_attempt)) != job.attempt_number
        ):
            prior_key = (
                str(prior_target_name),
                str(prior_job_target["publication_target_version"]),
                job.id,
                int(str(prior_attempt)),
            )
            retired.setdefault(
                prior_key,
                SearchTargetPublication(
                    target=SearchTarget(prior_key[0], prior_key[1]),
                    record_count=1,
                    publication_job_id=job.id,
                    publication_attempt=prior_key[3],
                ),
            )
        recovery = SearchRecoveryRepository()
        for publication in retired.values():
            await recovery.enqueue_cleanup(
                session,
                job.tenant_id,
                job.collection_id,
                job.document_version_id,
                publication,
                job.document_version_id,
            )
        await session.execute(
            text(
                """
                UPDATE index_publications
                SET deleted_at = COALESCE(deleted_at, now())
                WHERE tenant_id = :tenant_id
                  AND collection_id = :collection_id
                  AND document_version_id = :version_id
                  AND deleted_at IS NULL
                  AND (publication_job_id, publication_attempt)
                      IS DISTINCT FROM (:job_id, :attempt_number)
                """
            ),
            {
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "version_id": job.document_version_id,
                "job_id": job.id,
                "attempt_number": job.attempt_number,
            },
        )
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET publication_target_name = :target_name,
                    publication_target_version = :target_version,
                    publication_target_attempt = :attempt_number,
                    updated_at = now()
                WHERE id = :job_id
                """
            ),
            {
                "target_name": target.name,
                "target_version": target.version,
                "attempt_number": job.attempt_number,
                "job_id": job.id,
            },
        )

    async def prepare_index(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        worker_id: str,
        artifact: PublishedArtifact,
        target_name: str,
        target_version: str,
        publications: Iterable[PublicationRecord],
    ) -> PreparedIndexPublication:
        locked = await self._locked_job_version(session, job, worker_id)
        chunk_artifact_id = await self._chunk_artifact_id(session, job)
        target = SearchTarget(name=target_name, version=target_version)
        await self._insert_derived_artifact(
            session,
            job,
            artifact,
            chunk_artifact_id,
            search_target=target,
        )
        for publication in publications:
            await session.execute(
                text(
                    """
                    INSERT INTO index_publications (
                        id, tenant_id, collection_id, document_version_id,
                        chunk_id, publication_kind, target_name, target_version,
                        external_record_id, published_at, publication_job_id,
                        publication_attempt, activated_at
                    ) VALUES (
                        :id, :tenant_id, :collection_id, :version_id,
                        :chunk_id, :kind, :target_name, :target_version,
                        :external_id, now(), :job_id, :attempt_number, NULL
                    ) ON CONFLICT ON CONSTRAINT uq_index_publications_target
                    DO UPDATE SET deleted_at = NULL, published_at = now(),
                        external_record_id = EXCLUDED.external_record_id,
                        publication_job_id = EXCLUDED.publication_job_id,
                        publication_attempt = EXCLUDED.publication_attempt,
                        activated_at = NULL
                    """
                ),
                {
                    "id": uuid4(),
                    "tenant_id": job.tenant_id,
                    "collection_id": job.collection_id,
                    "version_id": job.document_version_id,
                    "chunk_id": publication.chunk_id,
                    "kind": publication.publication_kind,
                    "target_name": target_name,
                    "target_version": target_version,
                    "external_id": publication.external_record_id,
                    "job_id": job.id,
                    "attempt_number": job.attempt_number,
                },
            )
        evidence = await require_publication_evidence(session, job, target)
        return PreparedIndexPublication(
            locked_version=dict(locked),
            evidence=evidence,
            source_targets=(),
        )

    async def lock_index_for_activation(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        worker_id: str,
        target: SearchTarget,
    ) -> PreparedIndexPublication:
        source_targets: tuple[SearchTargetPublication, ...] = ()
        if job.source_document_version_id != job.document_version_id:
            source = (
                await session.execute(
                    text(
                        """
                        SELECT state
                        FROM document_versions version
                        WHERE version.tenant_id = :tenant_id
                          AND version.collection_id = :collection_id
                          AND version.id = :source_version_id
                        FOR UPDATE
                        """
                    ),
                    {
                        "tenant_id": job.tenant_id,
                        "collection_id": job.collection_id,
                        "source_version_id": job.source_document_version_id,
                    },
                )
            ).mappings().one()
            if source["state"] not in {"ready", "ready_with_warnings"}:
                raise ValueError("reprocess source is no longer active")
            source_rows = (
                await session.execute(
                    text(
                        """
                        SELECT target_name, target_version,
                          publication_job_id, publication_attempt,
                          count(DISTINCT chunk_id) AS record_count,
                          count(DISTINCT chunk_id) FILTER (
                            WHERE publication_kind = 'lexical'
                          ) AS lexical_count,
                          count(DISTINCT chunk_id) FILTER (
                            WHERE publication_kind = 'vector'
                          ) AS vector_count,
                          bool_and(activated_at IS NOT NULL) AS activated
                        FROM index_publications
                        WHERE tenant_id = :tenant_id
                          AND collection_id = :collection_id
                          AND document_version_id = :source_version_id
                          AND deleted_at IS NULL
                        GROUP BY target_name, target_version,
                                 publication_job_id, publication_attempt
                        ORDER BY target_name, target_version
                        """
                    ),
                    {
                        "tenant_id": job.tenant_id,
                        "collection_id": job.collection_id,
                        "source_version_id": job.source_document_version_id,
                    },
                )
            ).mappings().all()
            if not source_rows:
                raise ValueError("reprocess source has no active search records")
            versions_by_name: dict[str, set[str]] = {}
            publications: list[SearchTargetPublication] = []
            for row in source_rows:
                target_name = str(row["target_name"])
                target_version = str(row["target_version"])
                versions_by_name.setdefault(target_name, set()).add(target_version)
                record_count = int(str(row["record_count"]))
                if record_count < 1 or any(
                    int(str(row[count_name])) != record_count
                    for count_name in ("lexical_count", "vector_count")
                ):
                    raise ValueError("reprocess source search target is incomplete")
                if not bool(row["activated"]):
                    raise ValueError("reprocess source search target is not activated")
                publication_job_id = row["publication_job_id"]
                publication_attempt = row["publication_attempt"]
                publications.append(
                    SearchTargetPublication(
                        target=SearchTarget(target_name, target_version),
                        record_count=record_count,
                        publication_job_id=(
                            UUID(str(publication_job_id))
                            if publication_job_id is not None
                            else None
                        ),
                        publication_attempt=(
                            int(str(publication_attempt))
                            if publication_attempt is not None
                            else None
                        ),
                    )
                )
            if any(len(versions) > 1 for versions in versions_by_name.values()):
                raise ValueError("search target name has conflicting versions")
            source_targets = tuple(publications)
        locked = await self._locked_job_version(session, job, worker_id)
        evidence = await require_publication_evidence(session, job, target)
        return PreparedIndexPublication(
            locked_version=dict(locked),
            evidence=evidence,
            source_targets=source_targets,
        )

    async def finalize_index(
        self,
        session: AsyncSession,
        job: ClaimedPublicationJob,
        worker_id: str,
        prepared: PreparedIndexPublication,
    ) -> None:
        recovery = SearchRecoveryRepository()
        for source in prepared.source_targets:
            await recovery.enqueue_cleanup(
                session,
                job.tenant_id,
                job.collection_id,
                job.source_document_version_id,
                source,
                job.document_version_id,
            )
        await self._transition(
            session,
            prepared.locked_version,
            VersionState.READY,
            worker_id,
            f"worker:{job.id}:ready",
            publication_evidence=prepared.evidence,
        )
        if job.source_document_version_id != job.document_version_id:
            await supersede_source_version(session, job, worker_id)
        await self._complete_job(session, job)
        await self._finish_lifecycle_request(session, job.id, "succeeded")
