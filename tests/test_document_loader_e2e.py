import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.core.database import Database, normalize_database_url
from app.core.storage import ObjectStorage
from app.services.catalog.service import CatalogService
from app.services.ingestion.chunking import FastEmbedTokenSpanProvider
from app.services.ingestion.embedding_provider import FastEmbedProvider
from app.services.ingestion.publication_service import PublicationPipelineService
from app.services.ingestion.service import NativeIngestionService
from app.services.lifecycle.deletion_service import DeletionService
from app.services.lifecycle.reconciliation import ReconciliationService
from app.workers.celery_app import IngestionJobPayload, PipelineTask
from tests.document_loader_e2e_support import (
    assert_authorized_search,
    assert_version_states,
    configure_environment,
    delete_index,
    delete_tenant,
    job_id,
    post_lifecycle,
    request_lifecycle,
    required_environment,
    search_hit_count,
    submit_pdf,
    wait_for_search,
)
from tests.document_loader_scenario_support import (
    FailingDispatcher,
    RecordingDispatcher,
    artifact_keys,
    assert_durable_handoff,
    assert_legal_hold_defers_cleanup,
    assert_ready_publication,
    seed_stale_index_job,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATIVE_PDF = PROJECT_ROOT / "benchmarks" / "fixtures" / "native-simple-en-001.pdf"


@pytest.mark.document_loader_e2e
def test_pdf_submission_reaches_scoped_searchable_citation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = required_environment()
    database_url = values["TEST_DATABASE_URL"]
    index_name = f"rag-e2e-{uuid4().hex}"
    configure_environment(monkeypatch, values, index_name, tmp_path)
    settings = get_settings()
    wait_for_search(settings)
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    tenant_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": f"e2e-{tenant_id}"},
        )
    version_id: UUID | None = None
    database = Database(settings.database)
    storage = ObjectStorage(
        settings.storage,
        provider_timeouts=settings.provider_timeouts,
    )
    catalog_service = CatalogService(
        database,
        storage,
        RecordingDispatcher(),
        settings,
    )
    try:
        version_id, initial_job_id, collection_id = submit_pdf(
            tenant_id,
            NATIVE_PDF,
            catalog_service=catalog_service,
        )
        dispatcher = RecordingDispatcher()
        native_service = NativeIngestionService(
            database, storage, settings, dispatcher
        )
        native_payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=version_id,
            job_id=initial_job_id,
        )
        asyncio.run(native_service.process(native_payload, "e2e-native"))
        successor_id = dispatcher.job_id(PipelineTask.CHUNK_PROCESS)
        asyncio.run(
            NativeIngestionService(
                database,
                storage,
                settings,
                FailingDispatcher(),
            ).process(native_payload, "e2e-native-redelivery")
        )
        assert_durable_handoff(engine, initial_job_id, successor_id)

        publication_service = PublicationPipelineService(
            database,
            storage,
            settings,
            dispatcher,
            embedding_provider=FastEmbedProvider(settings.embedding),
            token_spans=FastEmbedTokenSpanProvider(settings.embedding),
        )
        chunk_payload = native_payload.model_copy(
            update={"job_id": successor_id}
        )
        asyncio.run(publication_service.process(chunk_payload, "chunk", "e2e-chunk"))
        embed_payload = native_payload.model_copy(
            update={"job_id": dispatcher.job_id(PipelineTask.EMBED_PROCESS)}
        )
        asyncio.run(publication_service.process(embed_payload, "embed", "e2e-embed"))
        index_payload = native_payload.model_copy(
            update={"job_id": dispatcher.job_id(PipelineTask.INDEX_PROCESS)}
        )
        asyncio.run(publication_service.process(index_payload, "index", "e2e-index"))
        asyncio.run(
            publication_service.process(index_payload, "index", "e2e-duplicate")
        )

        assert_ready_publication(engine, version_id)
        assert_authorized_search(settings, tenant_id, version_id)

        replacement_id = request_lifecycle(
            tenant_id,
            version_id,
            "reprocess",
            "document-loader-reprocess",
            catalog_service,
            "pdf-v2",
        )
        replacement_chunk_job = job_id(engine, replacement_id, "chunk")
        replacement_dispatcher = RecordingDispatcher()
        replacement_service = PublicationPipelineService(
            database,
            storage,
            settings,
            replacement_dispatcher,
            embedding_provider=FastEmbedProvider(settings.embedding),
            token_spans=FastEmbedTokenSpanProvider(settings.embedding),
        )
        replacement_payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=replacement_id,
            job_id=replacement_chunk_job,
        )
        asyncio.run(
            replacement_service.process(
                replacement_payload, "chunk", "e2e-reprocess-chunk"
            )
        )
        replacement_payload = replacement_payload.model_copy(
            update={
                "job_id": replacement_dispatcher.job_id(
                    PipelineTask.EMBED_PROCESS
                )
            }
        )
        asyncio.run(
            replacement_service.process(
                replacement_payload, "embed", "e2e-reprocess-embed"
            )
        )
        replacement_payload = replacement_payload.model_copy(
            update={
                "job_id": replacement_dispatcher.job_id(
                    PipelineTask.INDEX_PROCESS
                )
            }
        )
        asyncio.run(
            replacement_service.process(
                replacement_payload, "index", "e2e-reprocess-index"
            )
        )
        assert_version_states(engine, version_id, replacement_id)
        assert search_hit_count(settings, tenant_id, version_id) == 0
        assert search_hit_count(settings, tenant_id, replacement_id) > 0

        stale_job_id = seed_stale_index_job(engine, replacement_id)
        repair_dispatcher = RecordingDispatcher()
        findings = asyncio.run(
            ReconciliationService(
                database, repair_dispatcher, settings
            ).run_once()
        )
        assert any(
            finding.job_id == stale_job_id
            and finding.reason == "stale_dispatch"
            for finding in findings
        )
        assert repair_dispatcher.job_id(PipelineTask.INDEX_PROCESS) == stale_job_id

        replacement_object_keys = artifact_keys(engine, tenant_id, replacement_id)
        assert replacement_object_keys
        deleted_id = request_lifecycle(
            tenant_id,
            replacement_id,
            "delete",
            "document-loader-delete",
            catalog_service,
        )
        duplicate_delete = post_lifecycle(
            tenant_id,
            deleted_id,
            "delete",
            "document-loader-delete-duplicate",
            catalog_service,
        )
        assert duplicate_delete.status_code == 409
        blocked_reprocess = post_lifecycle(
            tenant_id,
            deleted_id,
            "reprocess",
            "document-loader-delete-race",
            catalog_service,
            "pdf-v3",
        )
        assert blocked_reprocess.status_code == 409
        with engine.begin() as connection:
            delete_request_id = connection.scalar(
                text(
                    """
                    SELECT id FROM lifecycle_requests
                    WHERE tenant_id = :tenant_id
                      AND idempotency_key = 'document-loader-delete'
                    """
                ),
                {"tenant_id": tenant_id},
            )
            assert isinstance(delete_request_id, UUID)
            connection.execute(
                text(
                    """
                    INSERT INTO object_cleanup_entries (
                        id, tenant_id, collection_id, document_version_id,
                        lifecycle_request_id, object_key
                    ) VALUES (
                        :id, :tenant_id, :collection_id, :version_id,
                        :request_id, :object_key
                    )
                    """
                ),
                [
                    {
                        "id": uuid4(),
                        "tenant_id": tenant_id,
                        "collection_id": collection_id,
                        "version_id": deleted_id,
                        "request_id": delete_request_id,
                        "object_key": object_key,
                    }
                    for object_key in replacement_object_keys
                ],
            )
            connection.execute(
                text(
                    """
                    UPDATE collections SET legal_hold = true
                    WHERE tenant_id = :tenant_id AND id = :collection_id
                    """
                ),
                {"tenant_id": tenant_id, "collection_id": collection_id},
            )
        deletion_payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=deleted_id,
            job_id=job_id(engine, deleted_id, "delete"),
        )
        rollout_settings = settings.model_copy(
            update={
                "search": settings.search.model_copy(
                    update={"index_name": f"{index_name}-next"}
                )
            }
        )
        asyncio.run(
            DeletionService(database, storage, rollout_settings).process(
                deletion_payload, "e2e-delete"
            )
        )
        assert search_hit_count(settings, tenant_id, deleted_id) == 0
        assert_legal_hold_defers_cleanup(engine, tenant_id, deleted_id)
        for object_key in replacement_object_keys:
            storage._client.head_object(
                Bucket=storage._bucket_name,
                Key=object_key,
            )

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE collections SET legal_hold = false
                    WHERE tenant_id = :tenant_id AND id = :collection_id
                    """
                ),
                {"tenant_id": tenant_id, "collection_id": collection_id},
            )
        retention_dispatcher = RecordingDispatcher()
        retention_settings = settings.model_copy(
            update={
                "lifecycle": settings.lifecycle.model_copy(
                    update={"reconcile_page_size": 1}
                )
            }
        )
        retention_findings = asyncio.run(
            ReconciliationService(
                database,
                retention_dispatcher,
                retention_settings,
            ).run_once()
        )
        assert any(
            finding.reason == "retention_cleanup_due"
            for finding in retention_findings
        )
        cleanup_payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=deleted_id,
            job_id=retention_dispatcher.job_id(PipelineTask.DELETE_DOCUMENT),
        )
        asyncio.run(
            DeletionService(database, storage, rollout_settings).process(
                cleanup_payload, "e2e-retention-cleanup"
            )
        )
        with engine.connect() as connection:
            remaining_artifacts = connection.scalar(
                text(
                    """
                    SELECT count(*) FROM artifacts
                    WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                    """
                ),
                {"tenant_id": tenant_id, "version_id": deleted_id},
            )
            pending_cleanup = connection.scalar(
                text(
                    """
                    SELECT count(*) FROM object_cleanup_entries
                    WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                      AND state = 'pending'
                    """
                ),
                {"tenant_id": tenant_id, "version_id": deleted_id},
            )
        assert remaining_artifacts == 0
        assert pending_cleanup == 0
        for object_key in replacement_object_keys:
            with pytest.raises(ClientError) as missing_object:
                storage._client.head_object(
                    Bucket=storage._bucket_name,
                    Key=object_key,
                )
            assert missing_object.value.response["Error"]["Code"] in {
                "404",
                "NoSuchKey",
                "NotFound",
            }
    finally:
        asyncio.run(database.close())
        storage.close()
        delete_index(settings)
        delete_tenant(engine, tenant_id)
        engine.dispose()
        get_settings.cache_clear()
