"""Infrastructure and cleanup helpers for native ingestion integration tests."""

import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import Database
from app.services.ingestion.repository import NativeJobRepository
from app.workers.celery_app import IngestionJobPayload, PipelineTask


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
        "P2_BROKER_URL",
        "P2_BROKER_USE_TLS",
    )
    values = {name: os.environ.get(name, "") for name in names}
    if any(not value for value in values.values()):
        pytest.skip("native ingestion integration infrastructure is not configured")
    return values


def configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    temporary_directory: Path,
) -> None:
    mappings = {
        "DATABASE__URL": environment["TEST_DATABASE_URL"],
        "STORAGE__ENDPOINT_URL": environment["P2_STORAGE_ENDPOINT_URL"],
        "STORAGE__BUCKET_NAME": environment["P2_STORAGE_BUCKET_NAME"],
        "STORAGE__ACCESS_KEY_ID": environment["P2_STORAGE_ACCESS_KEY_ID"],
        "STORAGE__SECRET_ACCESS_KEY": environment["P2_STORAGE_SECRET_ACCESS_KEY"],
        "STORAGE__USE_TLS": environment["P2_STORAGE_USE_TLS"],
        "STORAGE__SERVER_SIDE_ENCRYPTION": environment[
            "P2_STORAGE_SERVER_SIDE_ENCRYPTION"
        ],
        "BROKER__URL": environment["P2_BROKER_URL"],
        "BROKER__USE_TLS": environment["P2_BROKER_USE_TLS"],
        "WORKERS__JOB_TEMP_DIRECTORY": str(temporary_directory),
    }
    for name, value in mappings.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


async def simulate_abandoned_claim(
    database: Database,
    payload: IngestionJobPayload,
) -> None:
    repository = NativeJobRepository()
    async with database.transaction() as session:
        claimed = await repository.claim(
            session,
            payload.tenant_id,
            payload.document_version_id,
            payload.job_id,
            "worker-killed-before-processing",
            timedelta(seconds=-1),
        )
    assert claimed is not None
    assert claimed.attempt_number == 1


def delete_tenant(engine, tenant_id: UUID) -> None:
    with engine.begin() as connection:
        parameters = {"tenant_id": tenant_id}
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
                parameters,
            )
        connection.execute(
            text("DELETE FROM tenants WHERE id = :tenant_id"),
            parameters,
        )
