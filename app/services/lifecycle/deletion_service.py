"""Idempotent deletion orchestration across search, storage, and catalog."""

from app.core.config import Settings
from app.core.database import Database
from app.core.storage import ObjectStorage
from app.services.ingestion.errors import JobFenceLostError, RetryableIngestionError
from app.services.ingestion.search_adapter import (
    OpenSearchIndexAdapter,
    SearchIndexAdapter,
)
from app.services.ingestion.search_recovery import SearchRecoveryService
from app.services.lifecycle.deletion_repository import DeletionRepository
from app.workers.celery_app import IngestionJobPayload


class DeletionService:
    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        settings: Settings,
        index_adapter: SearchIndexAdapter | None = None,
        repository: DeletionRepository | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._settings = settings
        self._index_adapter = index_adapter
        self._repository = repository or DeletionRepository()

    async def process(
        self, payload: IngestionJobPayload, worker_id: str
    ) -> None:
        async with self._database.transaction() as session:
            deletion = await self._repository.claim(
                session,
                payload.tenant_id,
                payload.document_version_id,
                payload.job_id,
                worker_id,
                self._settings.lifecycle.delete_batch_size,
            )
        if deletion is None:
            return
        owns_adapter = self._index_adapter is None
        adapter = self._index_adapter or OpenSearchIndexAdapter(
            self._settings.search,
            self._settings.embedding.dimensions,
        )
        try:
            recovery = SearchRecoveryService(
                self._database,
                self._settings,
                adapter=adapter,
            )
            await recovery.run_once(
                self._settings.lifecycle.reconcile_page_size,
                worker_id,
                document_version_id=deletion.document_version_id,
            )
            async with self._database.transaction() as session:
                search_cleanup_complete = (
                    await self._repository.search_cleanup_complete(
                        session,
                        deletion,
                        worker_id,
                    )
                )
            if not search_cleanup_complete:
                raise RuntimeError("search deletion remains pending")
            while True:
                async with self._database.transaction() as session:
                    deletion = await self._repository.next_batch(
                        session,
                        deletion,
                        worker_id,
                        self._settings.lifecycle.delete_batch_size,
                    )
                for object_key in deletion.object_keys:
                    await self._storage.delete_key(object_key)
                async with self._database.transaction() as session:
                    has_more = await self._repository.complete_batch(
                        session, deletion, worker_id
                    )
                if not has_more:
                    break
        except RetryableIngestionError:
            raise
        except JobFenceLostError:
            return
        except Exception as error:
            async with self._database.transaction() as session:
                await self._repository.schedule_retry(
                    session,
                    deletion,
                    worker_id,
                    "DELETE_DEPENDENCY_UNAVAILABLE",
                    self._settings.workers.retry_base_seconds,
                )
            raise RetryableIngestionError(
                "DELETE_DEPENDENCY_UNAVAILABLE",
                self._settings.workers.retry_base_seconds,
            ) from error
        finally:
            if owns_adapter:
                await adapter.close()
