import asyncio
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from app.core.config import (
    DatabaseSettings,
    EmbeddingSettings,
    SearchSettings,
    Settings,
    StorageSettings,
    WorkerSettings,
    get_settings,
)
from app.core.database import Database, normalize_database_url
from app.core.storage import ObjectStorage
from app.services.ingestion.chunking import FastEmbedTokenSpanProvider
from app.services.ingestion.embedding_provider import FastEmbedProvider
from app.services.ingestion.publication_service import PublicationPipelineService
from app.workers.celery_app import IngestionJobPayload, PipelineTask
from tests.publication_integration_support import (
    RecordingDispatcher,
    assert_search_scope,
    delete_index,
    delete_tenant,
    dispatched_job,
    publish_normalized_artifact,
    recover_committed_search_activation,
    required_environment,
    seed_catalog,
    wait_for_search,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.publication_integration
def test_publication_pipeline_reaches_scoped_searchable_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = required_environment()
    database_url = values["TEST_DATABASE_URL"]
    if not database_url.rsplit("/", 1)[-1].endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")
    index_name = f"rag-p7-{uuid4().hex}"
    settings = Settings(
        database=DatabaseSettings(url=SecretStr(database_url)),
        storage=StorageSettings(
            endpoint_url=values["P2_STORAGE_ENDPOINT_URL"],
            bucket_name=values["P2_STORAGE_BUCKET_NAME"],
            access_key_id=SecretStr(values["P2_STORAGE_ACCESS_KEY_ID"]),
            secret_access_key=SecretStr(values["P2_STORAGE_SECRET_ACCESS_KEY"]),
            use_tls=values["P2_STORAGE_USE_TLS"].lower() == "true",
            server_side_encryption=cast(
                Literal["provider-default", "AES256", "aws:kms"],
                values["P2_STORAGE_SERVER_SIDE_ENCRYPTION"],
            ),
        ),
        workers=WorkerSettings(job_temp_directory=str(tmp_path)),
        embedding=EmbeddingSettings(
            model_directory=values["P2_EMBEDDING_MODEL_DIRECTORY"],
            batch_size=2,
        ),
        search=SearchSettings(
            endpoint_url=values["P2_SEARCH_ENDPOINT_URL"],
            username=values["P2_SEARCH_USERNAME"],
            password=SecretStr(values["P2_SEARCH_PASSWORD"]),
            verify_tls=values["P2_SEARCH_VERIFY_TLS"].lower() == "true",
            index_name=index_name,
        ),
    )
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    wait_for_search(settings.search)
    monkeypatch.setenv("DATABASE__URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")
    database = Database(settings.database)
    storage = ObjectStorage(
        settings.storage,
        provider_timeouts=settings.provider_timeouts,
    )
    dispatcher = RecordingDispatcher()
    tenant_id = uuid4()
    collection_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    chunk_job_id = uuid4()
    try:
        asyncio.run(
            publish_normalized_artifact(
                storage, settings, tmp_path, tenant_id, version_id
            )
        )
        normalized_path = tmp_path / "normalized-source.jsonl"
        seed_catalog(
            engine,
            tenant_id,
            collection_id,
            document_id,
            version_id,
            chunk_job_id,
            storage,
            normalized_path,
        )
        provider = FastEmbedProvider(settings.embedding)
        service = PublicationPipelineService(
            database,
            storage,
            settings,
            dispatcher,
            embedding_provider=provider,
            token_spans=FastEmbedTokenSpanProvider(settings.embedding),
        )
        chunk_payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=version_id,
            job_id=chunk_job_id,
        )
        asyncio.run(service.process(chunk_payload, "chunk", "p7-integration"))
        embed_job_id = dispatched_job(dispatcher, PipelineTask.EMBED_PROCESS)
        embed_payload = chunk_payload.model_copy(update={"job_id": embed_job_id})
        asyncio.run(service.process(embed_payload, "embed", "p7-integration"))
        index_job_id = dispatched_job(dispatcher, PipelineTask.INDEX_PROCESS)
        index_payload = chunk_payload.model_copy(update={"job_id": index_job_id})
        asyncio.run(service.process(index_payload, "index", "p7-integration"))
        asyncio.run(service.process(index_payload, "index", "duplicate-delivery"))

        with engine.connect() as connection:
            state = connection.scalar(
                text("SELECT state FROM document_versions WHERE id = :id"),
                {"id": version_id},
            )
            chunk_count = connection.scalar(
                text("SELECT count(*) FROM chunks WHERE document_version_id = :id"),
                {"id": version_id},
            )
            embedding_count = connection.scalar(
                text(
                    "SELECT count(*) FROM chunk_embeddings "
                    "WHERE document_version_id = :id"
                ),
                {"id": version_id},
            )
            publication_count = connection.scalar(
                text(
                    "SELECT count(*) FROM index_publications "
                    "WHERE document_version_id = :id AND deleted_at IS NULL"
                ),
                {"id": version_id},
            )
            pages = connection.execute(
                text(
                    "SELECT page_start, page_end FROM chunks "
                    "WHERE document_version_id = :id ORDER BY ordinal"
                ),
                {"id": version_id},
            ).all()
            diagnostics = connection.execute(
                text(
                    "SELECT stage, state, terminal_reason_code, attempt_count "
                    "FROM ingestion_jobs WHERE document_version_id = :id "
                    "ORDER BY created_at"
                ),
                {"id": version_id},
            ).all()
        assert state == "ready", diagnostics
        assert chunk_count and chunk_count > 0
        assert embedding_count == chunk_count
        assert publication_count == chunk_count * 2
        assert all(page_start == page_end == 1 for page_start, page_end in pages)
        assert_search_scope(settings.search, tenant_id, collection_id, version_id)
        with engine.begin() as connection:
            target_row = (
                connection.execute(
                    text(
                        """
                        SELECT target_name, target_version, publication_job_id,
                               publication_attempt
                        FROM index_publications
                        WHERE document_version_id = :version_id
                          AND deleted_at IS NULL
                        LIMIT 1
                        """
                    ),
                    {"version_id": version_id},
                )
            ).mappings().one()
            connection.execute(
                text(
                    "UPDATE index_publications SET activated_at = NULL "
                    "WHERE document_version_id = :version_id "
                    "AND deleted_at IS NULL"
                ),
                {"version_id": version_id},
            )
        asyncio.run(
            recover_committed_search_activation(
                database,
                settings,
                tenant_id,
                collection_id,
                version_id,
                int(chunk_count),
                target_row,
            )
        )
        assert_search_scope(settings.search, tenant_id, collection_id, version_id)
    finally:
        asyncio.run(database.close())
        storage.close()
        delete_index(settings.search)
        delete_tenant(engine, tenant_id)
        engine.dispose()
        get_settings.cache_clear()
