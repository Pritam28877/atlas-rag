"""Durable, bounded recovery for external search cleanup and activation."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import Database
from app.services.ingestion.publication_models import (
    PendingSearchActivation,
    SearchCleanupClaim,
    SearchTarget,
    SearchTargetPublication,
)
from app.services.ingestion.search_adapter import (
    OpenSearchIndexAdapter,
    SearchIndexAdapter,
)
from app.services.ingestion.search_recovery_sql import PENDING_ACTIVATIONS_SQL


class SearchRecoveryRepository:
    async def enqueue_cleanup(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        publication: SearchTargetPublication,
        activation_version_id: UUID | None,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO search_cleanup_entries (
                    id, tenant_id, collection_id, document_version_id,
                    activation_version_id, publication_job_id,
                    publication_attempt, target_name, target_version
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :activation_version_id, :publication_job_id,
                    :publication_attempt, :target_name, :target_version
                ) ON CONFLICT ON CONSTRAINT uq_search_cleanup_target DO NOTHING
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "collection_id": collection_id,
                "version_id": document_version_id,
                "activation_version_id": activation_version_id,
                "publication_job_id": publication.publication_job_id,
                "publication_attempt": publication.publication_attempt,
                "target_name": publication.target.name,
                "target_version": publication.target.version,
            },
        )

    async def claim_cleanup(
        self,
        session: AsyncSession,
        worker_id: str,
        lease_duration: timedelta,
        max_attempts: int,
        activation_version_id: UUID | None = None,
        document_version_id: UUID | None = None,
    ) -> SearchCleanupClaim | None:
        await session.execute(
            text(
                """
                WITH exhausted AS (
                    SELECT id FROM search_cleanup_entries
                    WHERE attempt_count >= :max_attempts
                      AND (
                        CAST(:activation_version_id AS uuid) IS NULL
                        OR activation_version_id = :activation_version_id
                      )
                      AND (
                        CAST(:document_version_id AS uuid) IS NULL
                        OR document_version_id = :document_version_id
                      )
                      AND (
                        state = 'pending'
                        OR (state = 'running' AND lease_expires_at <= now())
                      )
                    ORDER BY updated_at, id
                    LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                UPDATE search_cleanup_entries cleanup
                SET state = 'failed', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = now(),
                    last_reason_code = COALESCE(
                        last_reason_code, 'SEARCH_CLEANUP_RETRY_EXHAUSTED'
                    )
                FROM exhausted WHERE cleanup.id = exhausted.id
                """
            ),
            {
                "max_attempts": max_attempts,
                "activation_version_id": activation_version_id,
                "document_version_id": document_version_id,
            },
        )
        row = (
            await session.execute(
                text(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM search_cleanup_entries
                        WHERE attempt_count < :max_attempts
                          AND (
                            state = 'pending'
                            OR (state = 'running' AND lease_expires_at <= now())
                          )
                          AND (
                            CAST(:activation_version_id AS uuid) IS NULL
                            OR activation_version_id = :activation_version_id
                          )
                          AND (
                            CAST(:document_version_id AS uuid) IS NULL
                            OR document_version_id = :document_version_id
                          )
                        ORDER BY updated_at, id
                        LIMIT 1 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE search_cleanup_entries cleanup
                    SET state = 'running', attempt_count = attempt_count + 1,
                        lease_owner = :worker_id,
                        lease_expires_at = now() + :lease_duration,
                        updated_at = now()
                    FROM candidate
                    WHERE cleanup.id = candidate.id
                    RETURNING cleanup.*
                    """
                ),
                {
                    "max_attempts": max_attempts,
                    "activation_version_id": activation_version_id,
                    "document_version_id": document_version_id,
                    "worker_id": worker_id,
                    "lease_duration": lease_duration,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        publication_job_id = row["publication_job_id"]
        publication_attempt = row["publication_attempt"]
        return SearchCleanupClaim(
            id=UUID(str(row["id"])),
            tenant_id=UUID(str(row["tenant_id"])),
            collection_id=UUID(str(row["collection_id"])),
            document_version_id=UUID(str(row["document_version_id"])),
            activation_version_id=(
                UUID(str(row["activation_version_id"]))
                if row["activation_version_id"] is not None
                else None
            ),
            target=SearchTarget(
                name=str(row["target_name"]),
                version=str(row["target_version"]),
            ),
            attempt_number=int(str(row["attempt_count"])),
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

    async def complete_cleanup(
        self,
        session: AsyncSession,
        claim: SearchCleanupClaim,
        worker_id: str,
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE search_cleanup_entries
                SET state = 'deleted', deleted_at = now(),
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_reason_code = NULL, updated_at = now()
                WHERE id = :id AND state = 'running'
                  AND lease_owner = :worker_id
                  AND attempt_count = :attempt_number
                """
            ),
            {
                "id": claim.id,
                "worker_id": worker_id,
                "attempt_number": claim.attempt_number,
            },
        )

    async def retry_cleanup(
        self,
        session: AsyncSession,
        claim: SearchCleanupClaim,
        worker_id: str,
        max_attempts: int,
        reason_code: str,
    ) -> None:
        state = "failed" if claim.attempt_number >= max_attempts else "pending"
        await session.execute(
            text(
                """
                UPDATE search_cleanup_entries
                SET state = :state, lease_owner = NULL,
                    lease_expires_at = NULL, last_reason_code = :reason_code,
                    updated_at = now()
                WHERE id = :id AND state = 'running'
                  AND lease_owner = :worker_id
                  AND attempt_count = :attempt_number
                """
            ),
            {
                "state": state,
                "reason_code": reason_code,
                "id": claim.id,
                "worker_id": worker_id,
                "attempt_number": claim.attempt_number,
            },
        )

    async def pending_activations(
        self,
        session: AsyncSession,
        limit: int,
        activation_version_id: UUID | None = None,
        document_version_id: UUID | None = None,
    ) -> tuple[PendingSearchActivation, ...]:
        rows = (
            await session.execute(
                text(PENDING_ACTIVATIONS_SQL),
                {
                    "activation_version_id": activation_version_id,
                    "document_version_id": document_version_id,
                    "limit": limit,
                },
            )
        ).mappings().all()
        return tuple(
            PendingSearchActivation(
                tenant_id=UUID(str(row["tenant_id"])),
                collection_id=UUID(str(row["collection_id"])),
                document_version_id=UUID(str(row["document_version_id"])),
                target=SearchTarget(
                    name=str(row["target_name"]),
                    version=str(row["target_version"]),
                ),
                record_count=int(str(row["record_count"])),
                publication_job_id=UUID(str(row["publication_job_id"])),
                publication_attempt=int(str(row["publication_attempt"])),
            )
            for row in rows
        )

    async def complete_activation(
        self,
        session: AsyncSession,
        activation: PendingSearchActivation,
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE index_publications
                SET activated_at = COALESCE(activated_at, now())
                WHERE tenant_id = :tenant_id
                  AND collection_id = :collection_id
                  AND document_version_id = :version_id
                  AND target_name = :target_name
                  AND target_version = :target_version
                  AND publication_job_id = :publication_job_id
                  AND publication_attempt = :publication_attempt
                  AND deleted_at IS NULL
                """
            ),
            {
                "tenant_id": activation.tenant_id,
                "collection_id": activation.collection_id,
                "version_id": activation.document_version_id,
                "target_name": activation.target.name,
                "target_version": activation.target.version,
                "publication_job_id": activation.publication_job_id,
                "publication_attempt": activation.publication_attempt,
            },
        )


class SearchRecoveryService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        adapter: SearchIndexAdapter | None = None,
        repository: SearchRecoveryRepository | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._adapter = adapter
        self._repository = repository or SearchRecoveryRepository()

    async def run_once(
        self,
        limit: int,
        worker_id: str,
        activation_version_id: UUID | None = None,
        document_version_id: UUID | None = None,
    ) -> tuple[int, int]:
        if limit < 1:
            return 0, 0
        owns_adapter = self._adapter is None
        adapter = self._adapter or OpenSearchIndexAdapter(
            self._settings.search,
            self._settings.embedding.dimensions,
        )
        cleaned = 0
        activated = 0
        try:
            while cleaned < limit:
                async with self._database.transaction() as session:
                    claim = await self._repository.claim_cleanup(
                        session,
                        worker_id,
                        timedelta(seconds=self._settings.lifecycle.stale_job_seconds),
                        self._settings.workers.job_max_attempts,
                        activation_version_id,
                        document_version_id,
                    )
                if claim is None:
                    break
                try:
                    await adapter.delete_version(
                        claim.tenant_id,
                        claim.collection_id,
                        claim.document_version_id,
                        claim.target,
                        claim.publication_job_id,
                        claim.publication_attempt,
                    )
                except Exception:
                    async with self._database.transaction() as session:
                        await self._repository.retry_cleanup(
                            session,
                            claim,
                            worker_id,
                            self._settings.workers.job_max_attempts,
                            "SEARCH_CLEANUP_UNAVAILABLE",
                        )
                    raise
                async with self._database.transaction() as session:
                    await self._repository.complete_cleanup(session, claim, worker_id)
                cleaned += 1

            remaining = limit - cleaned
            if remaining > 0:
                async with self._database.transaction() as session:
                    activations = await self._repository.pending_activations(
                        session,
                        remaining,
                        activation_version_id,
                        document_version_id,
                    )
                for activation in activations:
                    await adapter.activate_version(
                        activation.tenant_id,
                        activation.collection_id,
                        activation.document_version_id,
                        activation.record_count,
                        activation.target,
                        activation.publication_job_id,
                        activation.publication_attempt,
                    )
                    async with self._database.transaction() as session:
                        await self._repository.complete_activation(
                            session,
                            activation,
                        )
                    activated += 1
        finally:
            if owns_adapter:
                await adapter.close()
        return cleaned, activated
