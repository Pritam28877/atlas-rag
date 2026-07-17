"""Chunk, embedding, and hybrid-index publication pipeline."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.core.database import Database
from app.core.storage import ArtifactKind, ObjectStorage, StorageIntegrityError
from app.services.catalog.enums import VersionState
from app.services.catalog.service import JobDispatcher
from app.services.ingestion.chunking import (
    TokenSpanProvider,
    iter_chunk_manifest,
)
from app.services.ingestion.embedding_artifacts import (
    load_embeddings,
)
from app.services.ingestion.embedding_provider import EmbeddingProvider
from app.services.ingestion.errors import JobFenceLostError, RetryableIngestionError
from app.services.ingestion.publication_activation import PublicationActivator
from app.services.ingestion.publication_artifact_stages import (
    PublicationArtifactStageRunner,
    remove_uncommitted_artifact,
)
from app.services.ingestion.publication_artifacts import (
    PublicationManifestWriter,
    iter_publication_manifest,
)
from app.services.ingestion.publication_models import (
    ClaimedPublicationJob,
    SearchTarget,
)
from app.services.ingestion.publication_repository import PublicationJobRepository
from app.services.ingestion.publication_retry import retry_delay, retry_or_fail
from app.services.ingestion.publication_storage import PublicationArtifactStore
from app.services.ingestion.repository_support import NextStageJob, defer_job_dispatch
from app.services.ingestion.search_adapter import (
    OpenSearchIndexAdapter,
    SearchIndexAdapter,
    SearchPublicationError,
    SearchRecord,
)
from app.services.ingestion.search_recovery import SearchRecoveryService
from app.workers.celery_app import IngestionJobPayload, PipelineTask
from app.workers.runtime import job_temp_directory

logger = logging.getLogger(__name__)


class PublicationPipelineService:
    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        settings: Settings,
        dispatcher: JobDispatcher,
        embedding_provider: EmbeddingProvider | None = None,
        token_spans: TokenSpanProvider | None = None,
        index_adapter: SearchIndexAdapter | None = None,
        repository: PublicationJobRepository | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._settings = settings
        self._dispatcher = dispatcher
        self._index_adapter = index_adapter
        self._repository = repository or PublicationJobRepository()
        self._artifacts = PublicationArtifactStore(storage, settings)
        self._artifact_stages = PublicationArtifactStageRunner(
            database,
            storage,
            settings,
            self._repository,
            self._artifacts,
            embedding_provider,
            token_spans,
        )

    async def process(
        self, payload: IngestionJobPayload, stage: str, worker_id: str
    ) -> None:
        lease_duration = timedelta(
            seconds=self._settings.workers.publication_timeout_seconds + 30
        )
        async with self._database.transaction() as session:
            job = await self._repository.claim(
                session,
                payload.tenant_id,
                payload.document_version_id,
                payload.job_id,
                worker_id,
                lease_duration,
            )
            successor = None
            if job is None:
                successor = await self._repository.pending_successor(
                    session,
                    payload.tenant_id,
                    payload.document_version_id,
                    payload.job_id,
                )
        if job is None:
            if successor is not None:
                await self._dispatch_or_defer(
                    payload.tenant_id,
                    payload.document_version_id,
                    successor,
                )
            return
        if job.stage != stage:
            raise ValueError("publication task does not match durable job stage")
        try:
            next_job = await self._run_stage(job, worker_id)
        except JobFenceLostError:
            return
        except ValueError:
            logger.exception(
                "publication input validation failed",
                extra={"stage": job.stage, "job_id": str(job.id)},
            )
            async with self._database.transaction() as session:
                await self._repository.fail_permanently(
                    session,
                    job,
                    worker_id,
                    VersionState.FAILED,
                    "PUBLICATION_INPUT_INVALID",
                )
            return
        except (
            BotoCoreError,
            ClientError,
            OSError,
            RuntimeError,
            SearchPublicationError,
            StorageIntegrityError,
        ) as error:
            retry_scheduled = await retry_or_fail(
                self._database,
                self._repository,
                self._settings,
                job,
                worker_id,
            )
            if retry_scheduled:
                raise RetryableIngestionError(
                    "PUBLICATION_DEPENDENCY_UNAVAILABLE",
                    retry_delay(self._settings, job),
                ) from error
            return
        if next_job is not None:
            await self._dispatch_or_defer(
                job.tenant_id,
                job.document_version_id,
                next_job,
            )

    async def _dispatch_or_defer(
        self,
        tenant_id: UUID,
        document_version_id: UUID,
        next_job: NextStageJob,
    ) -> None:
        task = {
            "embed": PipelineTask.EMBED_PROCESS,
            "index": PipelineTask.INDEX_PROCESS,
        }[next_job.stage]
        try:
            await self._dispatcher.dispatch(
                task,
                tenant_id,
                document_version_id,
                next_job.id,
            )
        except Exception:
            logger.warning(
                "deferred committed publication successor after dispatch failure",
                extra={"job_id": str(next_job.id), "stage": next_job.stage},
            )
            async with self._database.transaction() as session:
                await defer_job_dispatch(
                    session,
                    tenant_id,
                    document_version_id,
                    next_job,
                    "PUBLICATION_DISPATCH_UNAVAILABLE",
                    timedelta(seconds=self._settings.workers.retry_base_seconds),
                )

    async def _run_stage(
        self, job: ClaimedPublicationJob, worker_id: str
    ) -> NextStageJob | None:
        if job.stage == "chunk":
            return await self._artifact_stages.chunk(job, worker_id)
        if job.stage == "embed":
            return await self._artifact_stages.embed(job, worker_id)
        await self._index(job, worker_id)
        return None

    async def _index(self, job: ClaimedPublicationJob, worker_id: str) -> None:
        owns_adapter = self._index_adapter is None
        adapter = self._index_adapter or OpenSearchIndexAdapter(
            self._settings.search,
            self._settings.embedding.dimensions,
        )
        try:
            await self._publish_index(job, worker_id, adapter)
        finally:
            if owns_adapter:
                await adapter.close()

    async def _publish_index(
        self,
        job: ClaimedPublicationJob,
        worker_id: str,
        adapter: SearchIndexAdapter,
    ) -> None:
        with job_temp_directory(
            Path(self._settings.workers.job_temp_directory), job.id
        ) as directory:
            chunk_path = directory / "chunks.jsonl"
            embedding_path = directory / "embeddings.jsonl"
            publication_path = directory / "index-publications.jsonl"
            await self._artifacts.download_chunk_manifest(job, chunk_path)
            await self._artifacts.download_embedding_manifest(job, embedding_path)
            embeddings = load_embeddings(
                embedding_path,
                job.tenant_id,
                job.document_version_id,
                self._settings.ingestion.max_chunks,
            )
            writer = PublicationManifestWriter(
                publication_path,
                job.tenant_id,
                job.document_version_id,
                adapter.target_name,
                adapter.target_version,
            )
            target = SearchTarget(
                name=adapter.target_name,
                version=adapter.target_version,
            )
            async with self._database.transaction() as session:
                await self._repository.begin_index_attempt(
                    session,
                    job,
                    worker_id,
                    target,
                )
            recovery = SearchRecoveryService(
                self._database,
                self._settings,
                adapter=adapter,
            )
            await recovery.run_once(
                self._settings.lifecycle.reconcile_page_size,
                worker_id,
                activation_version_id=job.document_version_id,
            )
            async with self._database.transaction() as session:
                cleanup_complete = await self._repository.index_cleanup_complete(
                    session,
                    job,
                )
            if not cleanup_complete:
                raise SearchPublicationError(
                    "prior search publication cleanup remains pending"
                )
            try:
                batch: list[SearchRecord] = []
                for chunk in iter_chunk_manifest(
                    chunk_path, job.tenant_id, job.document_version_id
                ):
                    embedding = embeddings.pop(chunk.chunk_id, None)
                    if embedding is None:
                        raise ValueError("chunk embedding coverage is incomplete")
                    batch.append(
                        SearchRecord(
                            tenant_id=job.tenant_id,
                            collection_id=job.collection_id,
                            document_version_id=job.document_version_id,
                            chunk=chunk,
                            embedding=embedding,
                            publication_job_id=job.id,
                            publication_attempt=job.attempt_number,
                            pipeline_profile=job.pipeline_profile,
                            embedding_provider=self._settings.embedding.provider,
                            embedding_model=self._settings.embedding.model_name,
                            embedding_model_version=(
                                self._settings.embedding.model_version
                            ),
                        )
                    )
                    if len(batch) == self._settings.embedding.batch_size:
                        for publication in await adapter.publish(batch):
                            writer.write(publication)
                        batch.clear()
                if batch:
                    for publication in await adapter.publish(batch):
                        writer.write(publication)
                if embeddings:
                    raise ValueError("embedding manifest contains unknown chunks")
                publication_count = writer.finish()
            finally:
                writer.close()
            if publication_count < 2 or publication_count % 2:
                raise ValueError("index publication manifest is incomplete")
            artifact = await self._artifacts.upload(
                job,
                publication_path,
                ArtifactKind.INDEX_MANIFEST,
                "opensearch",
                adapter.target_version,
            )
            expected_records = publication_count // 2
            activator = PublicationActivator(
                self._database,
                self._repository,
                adapter,
                self._settings,
            )
            preparation_committed = False
            try:
                async with self._database.transaction() as session:
                    await self._repository.prepare_index(
                        session,
                        job,
                        worker_id,
                        artifact,
                        adapter.target_name,
                        adapter.target_version,
                        iter_publication_manifest(
                            publication_path,
                            job.tenant_id,
                            job.document_version_id,
                        ),
                    )
                preparation_committed = True
                await activator.activate(
                    job,
                    worker_id,
                    expected_records,
                )
            except Exception:
                if not preparation_committed:
                    await self._remove_uncommitted_artifact(artifact.object_key)
                raise

    async def _remove_uncommitted_artifact(self, object_key: str) -> None:
        await remove_uncommitted_artifact(self._storage, object_key)
