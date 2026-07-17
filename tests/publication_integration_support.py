"""Platform setup, seed, recovery, and cleanup for publication integration."""

import contextlib
import os
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from app.core.config import SearchSettings, Settings
from app.core.database import Database
from app.core.storage import ArtifactKind, ObjectStorage
from app.services.ingestion.artifacts import PageArtifactWriter, file_sha256
from app.services.ingestion.models import (
    DocumentRoute,
    InspectionReport,
    NormalizedPage,
    PageOrigin,
    TextBlock,
)
from app.services.ingestion.publication_models import SearchTarget
from app.services.ingestion.search_adapter import OpenSearchIndexAdapter
from app.services.ingestion.search_recovery import SearchRecoveryService
from app.workers.celery_app import PipelineTask


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[PipelineTask, UUID, UUID, UUID]] = []

    async def dispatch(
        self,
        task: PipelineTask,
        tenant_id: UUID,
        version_id: UUID,
        job_id: UUID,
    ) -> None:
        self.calls.append((task, tenant_id, version_id, job_id))


def required_environment() -> dict[str, str]:
    names = (
        "TEST_DATABASE_URL",
        "P2_STORAGE_ENDPOINT_URL",
        "P2_STORAGE_BUCKET_NAME",
        "P2_STORAGE_ACCESS_KEY_ID",
        "P2_STORAGE_SECRET_ACCESS_KEY",
        "P2_STORAGE_USE_TLS",
        "P2_STORAGE_SERVER_SIDE_ENCRYPTION",
        "P2_SEARCH_ENDPOINT_URL",
        "P2_SEARCH_USERNAME",
        "P2_SEARCH_PASSWORD",
        "P2_SEARCH_VERIFY_TLS",
        "P2_EMBEDDING_MODEL_DIRECTORY",
    )
    values = {name: os.environ.get(name, "") for name in names}
    if any(not value for value in values.values()):
        pytest.skip("publication integration infrastructure is not configured")
    return values


async def recover_committed_search_activation(
    database: Database,
    settings: Settings,
    tenant_id: UUID,
    collection_id: UUID,
    version_id: UUID,
    expected_records: int,
    target_row: Mapping[str, object],
) -> None:
    target = SearchTarget(
        name=str(target_row["target_name"]),
        version=str(target_row["target_version"]),
    )
    publication_job_id = UUID(str(target_row["publication_job_id"]))
    publication_attempt = int(str(target_row["publication_attempt"]))
    adapter = OpenSearchIndexAdapter(
        settings.search,
        settings.embedding.dimensions,
    )
    try:
        await adapter.deactivate_version(
            tenant_id=tenant_id,
            collection_id=collection_id,
            document_version_id=version_id,
            expected_records=expected_records,
            target=target,
            publication_job_id=publication_job_id,
            publication_attempt=publication_attempt,
        )
        recovery = SearchRecoveryService(database, settings, adapter=adapter)
        assert await recovery.run_once(
            10,
            "publication-recovery-integration",
            activation_version_id=version_id,
        ) == (0, 1)
    finally:
        await adapter.close()


async def publish_normalized_artifact(
    storage: ObjectStorage,
    settings: Settings,
    temporary_directory: Path,
    tenant_id: UUID,
    version_id: UUID,
) -> None:
    path = temporary_directory / "normalized-source.jsonl"
    writer = PageArtifactWriter(path, version_id, "integration", "v1")
    page = NormalizedPage(
        page_number=1,
        origin=PageOrigin.NATIVE,
        text="Stable first page citation for authorized retrieval.",
        blocks=(
            TextBlock(
                ordinal=0,
                text="Stable first page citation for authorized retrieval.",
            ),
        ),
        language_hint="eng",
        quality_score=1,
    )
    writer.write_page(page)
    writer.finish(
        InspectionReport(
            parser_name="integration",
            parser_version="v1",
            page_count=1,
            route=DocumentRoute.NATIVE,
            native_page_count=1,
            ocr_page_numbers=(),
            extracted_text_chars=len(page.text),
        )
    )
    checksum = file_sha256(path)
    reference = storage.artifact_reference(
        tenant_id, version_id, ArtifactKind.NORMALIZED_DOCUMENT, "v1", checksum
    )
    await storage.upload_from_path(
        reference, path, "application/x-ndjson", settings.ingestion.normalized_max_bytes
    )


def seed_catalog(
    engine,
    tenant_id: UUID,
    collection_id: UUID,
    document_id: UUID,
    version_id: UUID,
    chunk_job_id: UUID,
    storage: ObjectStorage,
    normalized_path: Path,
) -> None:
    source_checksum = "0" * 64
    normalized_checksum = file_sha256(normalized_path)
    original = storage.original_reference(tenant_id, version_id, source_checksum)
    normalized = storage.artifact_reference(
        tenant_id,
        version_id,
        ArtifactKind.NORMALIZED_DOCUMENT,
        "v1",
        normalized_checksum,
    )
    original_artifact_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": f"p7-{tenant_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO collections (id, tenant_id, name, upload_max_bytes, "
                "document_quota, storage_quota_bytes) VALUES "
                "(:id, :tenant, 'p7', 1048576, 10, 10485760)"
            ),
            {"id": collection_id, "tenant": tenant_id},
        )
        connection.execute(
            text(
                "INSERT INTO source_documents "
                "(id, tenant_id, collection_id, source_key, display_name) VALUES "
                "(:id, :tenant, :collection, 'p7.pdf', 'p7.pdf')"
            ),
            {"id": document_id, "tenant": tenant_id, "collection": collection_id},
        )
        connection.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, collection_id, "
                "document_id, version_number, idempotency_key, content_sha256, "
                "size_bytes, page_count, detected_mime, object_key, "
                "pipeline_profile, state) VALUES (:id, :tenant, :collection, "
                ":document, 1, 'p7-integration', :checksum, 1, 1, "
                "'application/pdf', :object_key, 'pdf-v1', 'chunking')"
            ),
            {
                "id": version_id,
                "tenant": tenant_id,
                "collection": collection_id,
                "document": document_id,
                "checksum": source_checksum,
                "object_key": original.key,
            },
        )
        connection.execute(
            text(
                "INSERT INTO artifacts (id, tenant_id, collection_id, "
                "document_version_id, artifact_type, object_key, generator_name, "
                "generator_version, checksum_sha256, size_bytes) VALUES "
                "(:id, :tenant, :collection, :version, 'original_pdf', :key, "
                "'intake', 'v1', :checksum, 1)"
            ),
            {
                "id": original_artifact_id,
                "tenant": tenant_id,
                "collection": collection_id,
                "version": version_id,
                "key": original.key,
                "checksum": source_checksum,
            },
        )
        connection.execute(
            text(
                "INSERT INTO artifacts (id, tenant_id, collection_id, "
                "document_version_id, source_artifact_id, artifact_type, object_key, "
                "generator_name, generator_version, checksum_sha256, size_bytes) "
                "VALUES (:id, :tenant, :collection, :version, :source, "
                "'normalized_document', :key, 'integration', 'v1', :checksum, :size)"
            ),
            {
                "id": uuid4(),
                "tenant": tenant_id,
                "collection": collection_id,
                "version": version_id,
                "source": original_artifact_id,
                "key": normalized.key,
                "checksum": normalized_checksum,
                "size": normalized_path.stat().st_size,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ingestion_jobs (id, tenant_id, collection_id, "
                "document_version_id, stage, max_attempts) VALUES "
                "(:id, :tenant, :collection, :version, 'chunk', 3)"
            ),
            {
                "id": chunk_job_id,
                "tenant": tenant_id,
                "collection": collection_id,
                "version": version_id,
            },
        )


def dispatched_job(dispatcher: RecordingDispatcher, task: PipelineTask) -> UUID:
    matching_jobs = [call[3] for call in dispatcher.calls if call[0] is task]
    assert len(matching_jobs) == 1
    return matching_jobs[0]


def assert_search_scope(
    settings: SearchSettings,
    tenant_id: UUID,
    collection_id: UUID,
    version_id: UUID,
) -> None:
    assert settings.endpoint_url is not None
    assert settings.username is not None
    assert settings.password is not None
    authentication = (settings.username, settings.password.get_secret_value())
    filters = [
        {"term": {"tenant_id": str(tenant_id)}},
        {"term": {"collection_id": str(collection_id)}},
        {"term": {"document_version_id": str(version_id)}},
        {"term": {"publication_complete": True}},
    ]
    with httpx.Client(
        auth=authentication, verify=settings.verify_tls, timeout=30
    ) as client:
        response = client.post(
            f"{settings.endpoint_url}/{settings.index_name}/_search",
            json={"query": {"bool": {"filter": filters}}},
        )
        assert response.is_success, response.text
        assert response.json()["hits"]["total"]["value"] > 0
        filters[0] = {"term": {"tenant_id": str(uuid4())}}
        denied = client.post(
            f"{settings.endpoint_url}/{settings.index_name}/_search",
            json={"query": {"bool": {"filter": filters}}},
        )
    assert denied.is_success
    assert denied.json()["hits"]["total"]["value"] == 0


def delete_index(settings: SearchSettings) -> None:
    if settings.endpoint_url and settings.username and settings.password:
        with contextlib.suppress(httpx.HTTPError):
            with httpx.Client(
                auth=(settings.username, settings.password.get_secret_value()),
                verify=settings.verify_tls,
                timeout=30,
            ) as client:
                client.delete(f"{settings.endpoint_url}/{settings.index_name}")


def wait_for_search(settings: SearchSettings) -> None:
    assert settings.endpoint_url and settings.username and settings.password
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(
                auth=(settings.username, settings.password.get_secret_value()),
                verify=settings.verify_tls,
                timeout=2,
            ) as client:
                response = client.get(f"{settings.endpoint_url}/_cluster/health")
            if response.is_success:
                return
        except httpx.HTTPError as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError("OpenSearch did not become ready") from last_error


def delete_tenant(engine, tenant_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "lifecycle_requests",
            "version_transition_events",
            "job_attempts",
            "ingestion_jobs",
            "index_publications",
            "chunk_embeddings",
            "chunks",
            "artifacts",
            "document_versions",
            "source_documents",
            "collection_memberships",
            "collections",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        connection.execute(
            text("DELETE FROM tenants WHERE id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
