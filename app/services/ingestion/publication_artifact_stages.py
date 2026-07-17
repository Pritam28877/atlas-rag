"""Bounded chunk and embedding artifact stages for the publication pipeline."""

import asyncio
import contextlib
from pathlib import Path

from app.core.config import Settings
from app.core.database import Database
from app.core.storage import ArtifactKind, ObjectStorage
from app.services.ingestion.chunking import (
    FastEmbedTokenSpanProvider,
    TokenSpanProvider,
    iter_chunk_manifest,
    write_chunk_manifest,
)
from app.services.ingestion.embedding_artifacts import (
    iter_embedding_manifest,
    write_embedding_manifest,
)
from app.services.ingestion.embedding_provider import (
    EmbeddingProvider,
    FastEmbedProvider,
)
from app.services.ingestion.errors import JobFenceLostError
from app.services.ingestion.publication_models import ClaimedPublicationJob
from app.services.ingestion.publication_repository import PublicationJobRepository
from app.services.ingestion.publication_storage import PublicationArtifactStore
from app.services.ingestion.repository_support import NextStageJob
from app.workers.runtime import job_temp_directory


async def remove_uncommitted_artifact(
    storage: ObjectStorage,
    object_key: str,
) -> None:
    """Best-effort removal for an object whose catalog transaction did not commit."""
    with contextlib.suppress(Exception):
        await storage.delete_key(object_key)


class PublicationArtifactStageRunner:
    """Execute the CPU/storage work for chunk and embedding stages."""

    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        settings: Settings,
        repository: PublicationJobRepository,
        artifacts: PublicationArtifactStore,
        embedding_provider: EmbeddingProvider | None,
        token_spans: TokenSpanProvider | None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._settings = settings
        self._repository = repository
        self._artifacts = artifacts
        self._embedding_provider = embedding_provider
        self._token_spans = token_spans

    async def chunk(
        self,
        job: ClaimedPublicationJob,
        worker_id: str,
    ) -> NextStageJob:
        token_spans = self._token_spans or FastEmbedTokenSpanProvider(
            self._settings.embedding
        )
        with job_temp_directory(
            Path(self._settings.workers.job_temp_directory), job.id
        ) as directory:
            normalized_path = directory / "normalized-pages.jsonl"
            manifest_path = directory / "chunks.jsonl"
            normalized_reference = self._storage.artifact_reference(
                job.tenant_id,
                job.normalized_document_version_id,
                ArtifactKind.NORMALIZED_DOCUMENT,
                job.normalized_generator_version,
                job.normalized_checksum_sha256,
            )
            await self._storage.download_to_path(
                normalized_reference,
                normalized_path,
                self._settings.ingestion.normalized_max_bytes,
            )
            await asyncio.to_thread(
                write_chunk_manifest,
                normalized_path,
                manifest_path,
                job.tenant_id,
                job.document_version_id,
                self._settings.ingestion,
                token_spans,
                job.source_document_version_id,
            )
            artifact = await self._artifacts.upload(
                job,
                manifest_path,
                ArtifactKind.CHUNK_MANIFEST,
                "page-token-window",
                "chunk-v1",
            )
            try:
                async with self._database.transaction() as session:
                    return await self._repository.succeed_chunk(
                        session,
                        job,
                        worker_id,
                        artifact,
                        iter_chunk_manifest(
                            manifest_path, job.tenant_id, job.document_version_id
                        ),
                        self._settings.workers.job_max_attempts,
                    )
            except JobFenceLostError:
                await remove_uncommitted_artifact(self._storage, artifact.object_key)
                raise

    async def embed(
        self,
        job: ClaimedPublicationJob,
        worker_id: str,
    ) -> NextStageJob:
        provider = self._embedding_provider or FastEmbedProvider(
            self._settings.embedding
        )
        with job_temp_directory(
            Path(self._settings.workers.job_temp_directory), job.id
        ) as directory:
            chunk_path = directory / "chunks.jsonl"
            embedding_path = directory / "embeddings.jsonl"
            await self._artifacts.download_chunk_manifest(job, chunk_path)
            await asyncio.to_thread(
                write_embedding_manifest,
                chunk_path,
                embedding_path,
                job.tenant_id,
                job.document_version_id,
                provider,
                self._settings.embedding.batch_size,
            )
            generator_version = f"fastembed-{provider.model_version}"
            artifact = await self._artifacts.upload(
                job,
                embedding_path,
                ArtifactKind.INDEX_MANIFEST,
                provider.name,
                generator_version,
            )
            try:
                async with self._database.transaction() as session:
                    return await self._repository.succeed_embedding(
                        session,
                        job,
                        worker_id,
                        artifact,
                        artifact.object_key,
                        provider.name,
                        provider.model_name,
                        provider.model_version,
                        provider.dimensions,
                        iter_embedding_manifest(
                            embedding_path, job.tenant_id, job.document_version_id
                        ),
                        self._settings.workers.job_max_attempts,
                    )
            except JobFenceLostError:
                await remove_uncommitted_artifact(self._storage, artifact.object_key)
                raise
