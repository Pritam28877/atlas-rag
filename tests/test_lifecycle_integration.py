import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from app.core.config import DatabaseSettings, Settings, get_settings
from app.core.database import Database, normalize_database_url
from app.services.lifecycle.deletion_service import DeletionService
from app.services.lifecycle.reconciliation import ReconciliationRepository
from app.workers.celery_app import IngestionJobPayload
from tests.lifecycle_integration_support import (
    RecordingSearchAdapter,
    RecordingStorage,
    _database_url,
    _delete_tenant,
    _seed_deleted_parent_with_surviving_child,
    _seed_ready_version_with_delete_job,
    _upgrade,
)


@pytest.mark.database_integration
def test_deletion_revokes_search_and_removes_catalog_owned_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    _upgrade(database_url, monkeypatch)
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    identifiers = _seed_ready_version_with_delete_job(engine)
    settings = Settings(database=DatabaseSettings(url=SecretStr(database_url)))
    database = Database(settings.database)
    storage = RecordingStorage()
    search = RecordingSearchAdapter()
    payload = IngestionJobPayload(
        tenant_id=identifiers["tenant_id"],
        document_version_id=identifiers["version_id"],
        job_id=identifiers["delete_job_id"],
    )
    try:
        service = DeletionService(
            database,
            storage,  # type: ignore[arg-type]
            settings,
            index_adapter=search,  # type: ignore[arg-type]
        )
        asyncio.run(service.process(payload, "lifecycle-integration"))

        with engine.connect() as connection:
            version_state = connection.scalar(
                text("SELECT state FROM document_versions WHERE id = :id"),
                {"id": identifiers["version_id"]},
            )
            job_state = connection.scalar(
                text("SELECT state FROM ingestion_jobs WHERE id = :id"),
                {"id": identifiers["delete_job_id"]},
            )
            request_state = connection.scalar(
                text("SELECT state FROM lifecycle_requests WHERE id = :id"),
                {"id": identifiers["request_id"]},
            )
            active_publications = connection.scalar(
                text(
                    "SELECT count(*) FROM index_publications "
                    "WHERE document_version_id = :id AND deleted_at IS NULL"
                ),
                {"id": identifiers["version_id"]},
            )
            cleanup_states = connection.execute(
                text(
                    "SELECT DISTINCT state FROM search_cleanup_entries "
                    "WHERE document_version_id = :id"
                ),
                {"id": identifiers["version_id"]},
            ).scalars().all()
            remaining_artifacts = connection.scalar(
                text("SELECT count(*) FROM artifacts WHERE document_version_id = :id"),
                {"id": identifiers["version_id"]},
            )

        assert version_state == "deleted"
        assert job_state == "succeeded"
        assert request_state == "succeeded"
        assert active_publications == 0
        assert cleanup_states == ["deleted"]
        assert remaining_artifacts == 0
        assert set(storage.deleted_keys) == {
            identifiers["original_key"],
            identifiers["chunk_key"],
            identifiers["index_key"],
        }
        assert len(search.deleted) == 1
        assert search.deleted[0][4:] == (None, None)
    finally:
        asyncio.run(database.close())
        _delete_tenant(engine, identifiers["tenant_id"])
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.database_integration
def test_reconciliation_defers_parent_cleanup_while_descendant_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    _upgrade(database_url, monkeypatch)
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    tenant_id = uuid4()
    _seed_deleted_parent_with_surviving_child(engine, tenant_id)
    settings = Settings(database=DatabaseSettings(url=SecretStr(database_url)))
    database = Database(settings.database)

    async def reconcile() -> tuple:
        async with database.transaction() as session:
            return await ReconciliationRepository().scan_and_repair(
                session,
                page_size=10,
                stale_after=timedelta(
                    seconds=settings.lifecycle.stale_job_seconds
                ),
                max_attempts=settings.workers.job_max_attempts,
            )

    try:
        findings = asyncio.run(reconcile())
        with engine.connect() as connection:
            delete_jobs = connection.scalar(
                text(
                    "SELECT count(*) FROM ingestion_jobs "
                    "WHERE tenant_id = :tenant_id AND stage = 'delete'"
                ),
                {"tenant_id": tenant_id},
            )
        assert findings == ()
        assert delete_jobs == 0
    finally:
        asyncio.run(database.close())
        _delete_tenant(engine, tenant_id)
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.database_integration
def test_reconciliation_fails_closed_for_corrupt_ready_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    _upgrade(database_url, monkeypatch)
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    identifiers = _seed_ready_version_with_delete_job(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ingestion_jobs WHERE id = :id"),
            {"id": identifiers["delete_job_id"]},
        )
        connection.execute(
            text("DELETE FROM lifecycle_requests WHERE id = :id"),
            {"id": identifiers["request_id"]},
        )
        connection.execute(
            text("DELETE FROM chunk_embeddings WHERE document_version_id = :id"),
            {"id": identifiers["version_id"]},
        )
    settings = Settings(database=DatabaseSettings(url=SecretStr(database_url)))
    database = Database(settings.database)

    async def reconcile() -> tuple:
        async with database.transaction() as session:
            return await ReconciliationRepository().scan_and_repair(
                session,
                page_size=10,
                stale_after=timedelta(
                    seconds=settings.lifecycle.stale_job_seconds
                ),
                max_attempts=settings.workers.job_max_attempts,
            )

    try:
        findings = asyncio.run(reconcile())
        with engine.connect() as connection:
            version = connection.execute(
                text(
                    "SELECT state, terminal_reason_code FROM document_versions "
                    "WHERE id = :id"
                ),
                {"id": identifiers["version_id"]},
            ).one()
            active_publications = connection.scalar(
                text(
                    "SELECT count(*) FROM index_publications "
                    "WHERE document_version_id = :id AND deleted_at IS NULL"
                ),
                {"id": identifiers["version_id"]},
            )
            cleanup_count = connection.scalar(
                text(
                    "SELECT count(*) FROM search_cleanup_entries "
                    "WHERE document_version_id = :id AND state = 'pending'"
                ),
                {"id": identifiers["version_id"]},
            )
        assert [finding.reason for finding in findings] == ["missing_embedding"]
        assert version == ("failed", "READY_PUBLICATION_CORRUPT")
        assert active_publications == 0
        assert cleanup_count == 1
    finally:
        asyncio.run(database.close())
        _delete_tenant(engine, identifiers["tenant_id"])
        engine.dispose()
        get_settings.cache_clear()
