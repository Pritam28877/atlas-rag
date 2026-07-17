"""Catalog-first search activation with durable external recovery."""

import logging

from app.core.config import Settings
from app.core.database import Database
from app.services.ingestion.publication_models import (
    ClaimedPublicationJob,
    SearchTarget,
    SearchTargetPublication,
)
from app.services.ingestion.publication_repository import PublicationJobRepository
from app.services.ingestion.search_adapter import SearchIndexAdapter
from app.services.ingestion.search_recovery import SearchRecoveryService

logger = logging.getLogger(__name__)


class PublicationActivator:
    def __init__(
        self,
        database: Database,
        repository: PublicationJobRepository,
        adapter: SearchIndexAdapter,
        settings: Settings,
    ) -> None:
        self._database = database
        self._repository = repository
        self._adapter = adapter
        self._settings = settings

    async def activate(
        self,
        job: ClaimedPublicationJob,
        worker_id: str,
        expected_records: int,
    ) -> tuple[SearchTargetPublication, ...]:
        """Commit catalog state first, then recover external visibility safely."""
        replacement_target = SearchTarget(
            name=self._adapter.target_name,
            version=self._adapter.target_version,
        )
        async with self._database.transaction() as session:
            prepared = await self._repository.lock_index_for_activation(
                session,
                job,
                worker_id,
                replacement_target,
            )
            if prepared.evidence.chunk_count != expected_records:
                raise ValueError("index publication record count changed")
            await self._repository.finalize_index(
                session,
                job,
                worker_id,
                prepared,
            )

        recovery_limit = min(
            self._settings.lifecycle.reconcile_page_size,
            job.max_attempts + len(prepared.source_targets) + 2,
        )
        recovery = SearchRecoveryService(
            self._database,
            self._settings,
            adapter=self._adapter,
        )
        try:
            await recovery.run_once(
                recovery_limit,
                worker_id,
                activation_version_id=job.document_version_id,
            )
        except Exception:
            logger.exception(
                "search activation deferred to durable recovery",
                extra={"job_id": str(job.id)},
            )
        return prepared.source_targets
