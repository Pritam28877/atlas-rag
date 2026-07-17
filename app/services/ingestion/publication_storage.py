"""Immutable object transfer helpers for publication stages."""

from pathlib import Path

from app.core.config import Settings
from app.core.storage import ArtifactKind, ObjectStorage
from app.services.ingestion.artifacts import file_sha256
from app.services.ingestion.publication_models import ClaimedPublicationJob
from app.services.ingestion.repository_support import PublishedArtifact


class PublicationArtifactStore:
    def __init__(self, storage: ObjectStorage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    async def download_chunk_manifest(
        self, job: ClaimedPublicationJob, destination: Path
    ) -> None:
        if not job.chunk_generator_version or not job.chunk_checksum_sha256:
            raise ValueError("chunk artifact provenance is incomplete")
        reference = self._storage.artifact_reference(
            job.tenant_id,
            job.document_version_id,
            ArtifactKind.CHUNK_MANIFEST,
            job.chunk_generator_version,
            job.chunk_checksum_sha256,
        )
        await self._storage.download_to_path(
            reference,
            destination,
            self._settings.ingestion.normalized_max_bytes,
        )

    async def download_embedding_manifest(
        self, job: ClaimedPublicationJob, destination: Path
    ) -> None:
        if not job.embedding_generator_version or not job.embedding_checksum_sha256:
            raise ValueError("embedding artifact provenance is incomplete")
        reference = self._storage.artifact_reference(
            job.tenant_id,
            job.document_version_id,
            ArtifactKind.INDEX_MANIFEST,
            job.embedding_generator_version,
            job.embedding_checksum_sha256,
        )
        await self._storage.download_to_path(
            reference,
            destination,
            self._settings.ingestion.normalized_max_bytes,
        )

    async def upload(
        self,
        job: ClaimedPublicationJob,
        path: Path,
        kind: ArtifactKind,
        generator_name: str,
        generator_version: str,
    ) -> PublishedArtifact:
        checksum = file_sha256(path)
        reference = self._storage.artifact_reference(
            job.tenant_id,
            job.document_version_id,
            kind,
            generator_version,
            checksum,
        )
        metadata = await self._storage.upload_from_path(
            reference,
            path,
            "application/x-ndjson",
            self._settings.ingestion.normalized_max_bytes,
        )
        return PublishedArtifact(
            artifact_type=kind.value.replace("-", "_"),
            object_key=reference.key,
            generator_name=generator_name,
            generator_version=generator_version,
            checksum_sha256=checksum,
            size_bytes=metadata.content_length,
        )
