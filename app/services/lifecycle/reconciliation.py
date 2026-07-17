"""Bounded reconciliation and idempotent repair dispatch."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import Database
from app.services.catalog.service import JobDispatcher
from app.services.ingestion.publication_models import (
    SearchTarget,
    SearchTargetPublication,
)
from app.services.ingestion.repository_support import NextStageJob, defer_job_dispatch
from app.services.ingestion.search_recovery import (
    SearchRecoveryRepository,
    SearchRecoveryService,
)
from app.services.lifecycle.reconciliation_sql import (
    CATALOG_FINDINGS_SQL,
    STALE_JOB_SCAN_SQL,
)
from app.workers.celery_app import PipelineTask

RECONCILIATION_HEARTBEAT_SERVICE = "document-loader-reconciliation"


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    reason: str
    tenant_id: UUID
    version_id: UUID
    job_id: UUID | None
    stage: str | None


class ReconciliationRepository:
    async def scan_and_repair(
        self,
        session: AsyncSession,
        page_size: int,
        stale_after: timedelta,
        max_attempts: int,
    ) -> tuple[ReconciliationFinding, ...]:
        retention_quota = max(1, page_size // 4)
        catalog_findings = await self._catalog_findings(
            session,
            retention_quota,
            max_attempts,
        )
        job_page_size = max(0, page_size - len(catalog_findings))
        rows = (
            await session.execute(
                text(STALE_JOB_SCAN_SQL),
                {"stale_after": stale_after, "page_size": job_page_size},
            )
        ).mappings().all()
        job_findings = [
            ReconciliationFinding(
                reason=str(row["reason"]),
                tenant_id=UUID(str(row["tenant_id"])),
                version_id=UUID(str(row["document_version_id"])),
                job_id=UUID(str(row["id"])),
                stage=str(row["stage"]),
            )
            for row in rows
        ]
        return tuple(catalog_findings + job_findings)

    async def _catalog_findings(
        self, session: AsyncSession, limit: int, max_attempts: int
    ) -> list[ReconciliationFinding]:
        rows = (
            await session.execute(
                text(CATALOG_FINDINGS_SQL),
                {"limit": limit},
            )
        ).mappings().all()
        findings: list[ReconciliationFinding] = []
        for row in rows:
            job_id = row["job_id"]
            stage = row["stage"]
            if row["reason"] == "retention_cleanup_due":
                job_id = uuid4()
                stage = "delete"
                request_id = uuid4()
                request_id = await session.scalar(
                    text(
                        """
                        INSERT INTO lifecycle_requests (
                            id, tenant_id, collection_id, document_version_id,
                            source_document_version_id, request_type,
                            idempotency_key, requested_by
                        ) VALUES (
                            :id, :tenant_id, :collection_id, :version_id,
                            :version_id, 'delete', :idempotency_key, 'reconciler'
                        ) ON CONFLICT (tenant_id, collection_id, idempotency_key)
                        DO UPDATE SET state = 'pending', completed_at = NULL
                        RETURNING id
                        """
                    ),
                    {
                        "id": request_id,
                        "tenant_id": row["tenant_id"],
                        "collection_id": row["collection_id"],
                        "version_id": row["version_id"],
                        "idempotency_key": f"retention:{row['version_id']}",
                    },
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO ingestion_jobs (
                            id, tenant_id, collection_id, document_version_id,
                            stage, generation, max_attempts, lifecycle_request_id,
                            sanitized_context
                        ) VALUES (
                            :id, :tenant_id, :collection_id, :version_id,
                            'delete', (
                              SELECT COALESCE(max(generation), -1) + 1
                              FROM ingestion_jobs
                              WHERE tenant_id = :tenant_id
                                AND document_version_id = :version_id
                            ),
                            :max_attempts, :request_id,
                            '{"reconciliation_reason":"retention_cleanup_due"}'::jsonb
                        )
                        """
                    ),
                    {
                        "id": job_id,
                        "tenant_id": row["tenant_id"],
                        "collection_id": row["collection_id"],
                        "version_id": row["version_id"],
                        "max_attempts": max_attempts,
                        "request_id": request_id,
                    },
                )
            else:
                await self._fail_corrupt_ready_version(session, row)
            findings.append(
                ReconciliationFinding(
                    reason=str(row["reason"]),
                    tenant_id=UUID(str(row["tenant_id"])),
                    version_id=UUID(str(row["version_id"])),
                    job_id=UUID(str(job_id)) if job_id else None,
                    stage=str(stage) if stage else None,
                )
            )
        return findings

    async def _fail_corrupt_ready_version(
        self,
        session: AsyncSession,
        row,
    ) -> None:
        publications = (
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
                    GROUP BY target_name, target_version,
                             publication_job_id, publication_attempt
                    """
                ),
                {
                    "tenant_id": row["tenant_id"],
                    "collection_id": row["collection_id"],
                    "version_id": row["version_id"],
                },
            )
        ).mappings().all()
        recovery = SearchRecoveryRepository()
        for publication in publications:
            publication_job_id = publication["publication_job_id"]
            publication_attempt = publication["publication_attempt"]
            await recovery.enqueue_cleanup(
                session,
                UUID(str(row["tenant_id"])),
                UUID(str(row["collection_id"])),
                UUID(str(row["version_id"])),
                SearchTargetPublication(
                    target=SearchTarget(
                        name=str(publication["target_name"]),
                        version=str(publication["target_version"]),
                    ),
                    record_count=max(
                        1,
                        int(str(publication["record_count"])),
                    ),
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
                ),
                None,
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
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "collection_id": row["collection_id"],
                "version_id": row["version_id"],
            },
        )
        transition = (
            await session.execute(
                text(
                    """
                    UPDATE document_versions
                    SET state = 'failed', state_revision = state_revision + 1,
                        terminal_reason_code = 'READY_PUBLICATION_CORRUPT',
                        updated_at = now()
                    WHERE tenant_id = :tenant_id
                      AND collection_id = :collection_id
                      AND id = :version_id
                      AND state IN ('ready', 'ready_with_warnings')
                    RETURNING state_revision - 1 AS previous_revision
                    """
                ),
                {
                    "tenant_id": row["tenant_id"],
                    "collection_id": row["collection_id"],
                    "version_id": row["version_id"],
                },
            )
        ).mappings().one_or_none()
        if transition is None:
            return
        previous_revision = int(str(transition["previous_revision"]))
        await session.execute(
            text(
                """
                INSERT INTO version_transition_events (
                    id, tenant_id, collection_id, document_version_id,
                    idempotency_key, from_state, to_state, from_revision,
                    to_revision, operation, actor_type, actor_id
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :event_key, :from_state, 'failed', :previous_revision,
                    :next_revision, 'reconcile', 'worker', 'reconciler'
                ) ON CONFLICT ON CONSTRAINT uq_version_transition_idempotency
                  DO NOTHING
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": row["tenant_id"],
                "collection_id": row["collection_id"],
                "version_id": row["version_id"],
                "event_key": f"reconcile:corrupt:{previous_revision + 1}",
                "from_state": row["version_state"],
                "previous_revision": previous_revision,
                "next_revision": previous_revision + 1,
            },
        )


class ReconciliationService:
    def __init__(
        self,
        database: Database,
        dispatcher: JobDispatcher,
        settings: Settings,
        repository: ReconciliationRepository | None = None,
    ) -> None:
        self._database = database
        self._dispatcher = dispatcher
        self._settings = settings
        self._repository = repository or ReconciliationRepository()

    async def run_once(self) -> tuple[ReconciliationFinding, ...]:
        page_size = self._settings.lifecycle.reconcile_page_size
        recovered = 0
        if self._settings.search.endpoint_url is not None:
            recovery_limit = max(1, page_size // 4)
            recovery = SearchRecoveryService(self._database, self._settings)
            cleaned, activated = await recovery.run_once(
                recovery_limit,
                RECONCILIATION_HEARTBEAT_SERVICE,
            )
            recovered = cleaned + activated
        remaining = max(0, page_size - recovered)
        findings: tuple[ReconciliationFinding, ...] = ()
        if remaining:
            async with self._database.transaction() as session:
                findings = await self._repository.scan_and_repair(
                    session,
                    remaining,
                    timedelta(seconds=self._settings.lifecycle.stale_job_seconds),
                    self._settings.workers.job_max_attempts,
                )
        for finding_index, finding in enumerate(findings):
            if finding.job_id is None or finding.stage is None:
                continue
            task = _task_for_stage(finding.stage)
            try:
                await self._dispatcher.dispatch(
                    task,
                    finding.tenant_id,
                    finding.version_id,
                    finding.job_id,
                )
            except Exception:
                await self._defer_undispatched(findings[finding_index:])
                raise
        async with self._database.transaction() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO service_heartbeats (service_name, updated_at)
                    VALUES (:service_name, now())
                    ON CONFLICT (service_name)
                    DO UPDATE SET updated_at = EXCLUDED.updated_at
                    """
                ),
                {"service_name": RECONCILIATION_HEARTBEAT_SERVICE},
            )
        return findings

    async def _defer_undispatched(
        self, findings: tuple[ReconciliationFinding, ...]
    ) -> None:
        retry_delay = timedelta(seconds=self._settings.workers.retry_base_seconds)
        async with self._database.transaction() as session:
            for finding in findings:
                if finding.job_id is None or finding.stage is None:
                    continue
                await defer_job_dispatch(
                    session,
                    finding.tenant_id,
                    finding.version_id,
                    NextStageJob(id=finding.job_id, stage=finding.stage),
                    "RECONCILIATION_DISPATCH_UNAVAILABLE",
                    retry_delay,
                )


def _task_for_stage(stage: str) -> PipelineTask:
    tasks = {
        "preflight": PipelineTask.NATIVE_PROCESS,
        "native_parse": PipelineTask.NATIVE_PROCESS,
        "ocr": PipelineTask.OCR_PROCESS,
        "chunk": PipelineTask.CHUNK_PROCESS,
        "embed": PipelineTask.EMBED_PROCESS,
        "index": PipelineTask.INDEX_PROCESS,
        "delete": PipelineTask.DELETE_DOCUMENT,
    }
    if stage not in tasks:
        raise ValueError("reconciler found an unsupported job stage")
    return tasks[stage]
