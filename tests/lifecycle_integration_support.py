"""Database seeds and fakes for lifecycle integration scenarios."""

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import get_settings
from app.services.ingestion.publication_models import SearchTarget

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecordingStorage:
    def __init__(self) -> None:
        self.deleted_keys: list[str] = []

    async def delete_key(self, object_key: str) -> None:
        self.deleted_keys.append(object_key)


class RecordingSearchAdapter:
    def __init__(self) -> None:
        self.deleted: list[tuple[UUID, UUID, UUID, SearchTarget, object, object]] = []

    async def delete_version(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        document_version_id: UUID,
        target: SearchTarget,
        publication_job_id=None,
        publication_attempt=None,
    ) -> None:
        self.deleted.append(
            (
                tenant_id,
                collection_id,
                document_version_id,
                target,
                publication_job_id,
                publication_attempt,
            )
        )


def _database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL")
    if value is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    if not value.rsplit("/", 1)[-1].endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")
    return value


def _upgrade(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE__URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")


def _seed_ready_version_with_delete_job(engine) -> dict[str, object]:
    ids = {
        "tenant_id": uuid4(),
        "collection_id": uuid4(),
        "document_id": uuid4(),
        "version_id": uuid4(),
        "chunk_artifact_id": uuid4(),
        "index_artifact_id": uuid4(),
        "chunk_id": uuid4(),
        "index_job_id": uuid4(),
        "request_id": uuid4(),
        "delete_job_id": uuid4(),
        "original_key": f"integration/original-{uuid4()}.pdf",
        "chunk_key": f"integration/chunks-{uuid4()}.jsonl",
        "index_key": f"integration/index-{uuid4()}.jsonl",
    }
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": ids["tenant_id"], "name": f"tenant-{ids['tenant_id']}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO collections (
                    id, tenant_id, name, retention_days, upload_max_bytes,
                    document_quota, storage_quota_bytes
                ) VALUES (:id, :tenant_id, :name, 0, 1000000, 10, 10000000)
                """
            ),
            {
                "id": ids["collection_id"],
                "tenant_id": ids["tenant_id"],
                "name": f"collection-{ids['collection_id']}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO source_documents (
                    id, tenant_id, collection_id, source_key, display_name
                ) VALUES (:id, :tenant_id, :collection_id, :source_key, 'test.pdf')
                """
            ),
            {
                "id": ids["document_id"],
                "tenant_id": ids["tenant_id"],
                "collection_id": ids["collection_id"],
                "source_key": f"source-{ids['document_id']}",
            },
        )
        _insert_version(connection, ids, state="indexing")
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, tenant_id, collection_id, document_version_id,
                    artifact_type, object_key, generator_name,
                    generator_version, checksum_sha256, size_bytes,
                    search_target_name, search_target_version
                ) VALUES
                  (:chunk_artifact_id, :tenant_id, :collection_id, :version_id,
                   'chunk_manifest', :chunk_key, 'chunker', 'v1',
                   repeat('b', 64), 10, NULL, NULL),
                  (:index_artifact_id, :tenant_id, :collection_id, :version_id,
                   'index_manifest', :index_key, 'opensearch', 'target-v1',
                   repeat('c', 64), 10, 'integration-index', 'target-v1')
                """
            ),
            ids,
        )
        connection.execute(
            text(
                """
                INSERT INTO chunks (
                    id, tenant_id, collection_id, document_version_id,
                    source_artifact_id, ordinal, page_start, page_end,
                    content_sha256, content_object_key, token_count,
                    chunker_name, chunker_version
                ) VALUES (
                    :chunk_id, :tenant_id, :collection_id, :version_id,
                    :chunk_artifact_id, 0, 1, 1, repeat('d', 64),
                    :chunk_key, 1, 'chunker', 'v1'
                )
                """
            ),
            ids,
        )
        connection.execute(
            text(
                """
                INSERT INTO chunk_embeddings (
                    id, tenant_id, collection_id, document_version_id,
                    chunk_id, provider, model_name, model_version, dimensions,
                    vector_object_key, vector_checksum_sha256
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :chunk_id, 'provider', 'model', 'v1', 2,
                    :index_key, repeat('e', 64)
                )
                """
            ),
            {**ids, "id": uuid4()},
        )
        connection.execute(
            text(
                """
                INSERT INTO ingestion_jobs (
                    id, tenant_id, collection_id, document_version_id,
                    stage, state, generation, attempt_count, max_attempts,
                    publication_target_name, publication_target_version,
                    publication_target_attempt
                ) VALUES (
                    :index_job_id, :tenant_id, :collection_id, :version_id,
                    'index', 'succeeded', 0, 1, 3,
                    'integration-index', 'target-v1', 1
                )
                """
            ),
            ids,
        )
        for publication_kind in ("lexical", "vector"):
            connection.execute(
                text(
                    """
                    INSERT INTO index_publications (
                        id, tenant_id, collection_id, document_version_id,
                        chunk_id, publication_kind, target_name, target_version,
                        external_record_id, published_at, publication_job_id,
                        publication_attempt, activated_at
                    ) VALUES (
                        :id, :tenant_id, :collection_id, :version_id,
                        :chunk_id, :kind, 'integration-index', 'target-v1',
                        :external_id, now(), :index_job_id, 1, now()
                    )
                    """
                ),
                {
                    **ids,
                    "id": uuid4(),
                    "kind": publication_kind,
                    "external_id": f"{ids['version_id']}:{publication_kind}",
                },
            )
        connection.execute(
            text(
                "UPDATE document_versions SET state = 'ready', state_revision = 1 "
                "WHERE id = :version_id"
            ),
            ids,
        )
        connection.execute(
            text(
                """
                INSERT INTO lifecycle_requests (
                    id, tenant_id, collection_id, document_version_id,
                    source_document_version_id, request_type,
                    idempotency_key, requested_by
                ) VALUES (
                    :request_id, :tenant_id, :collection_id, :version_id,
                    :version_id, 'delete', :key, 'integration-test'
                )
                """
            ),
            {**ids, "key": f"delete-{ids['version_id']}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO ingestion_jobs (
                    id, tenant_id, collection_id, document_version_id,
                    stage, generation, max_attempts, lifecycle_request_id
                ) VALUES (
                    :delete_job_id, :tenant_id, :collection_id, :version_id,
                    'delete', 0, 3, :request_id
                )
                """
            ),
            ids,
        )
    return ids


def _insert_version(connection, ids: dict[str, object], state: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO document_versions (
                id, tenant_id, collection_id, document_id, version_number,
                idempotency_key, content_sha256, size_bytes, object_key,
                pipeline_profile, state
            ) VALUES (
                :version_id, :tenant_id, :collection_id, :document_id, 1,
                :key, repeat('a', 64), 100, :original_key, 'integration', :state
            )
            """
        ),
        {**ids, "key": f"version-{ids['version_id']}", "state": state},
    )


def _seed_deleted_parent_with_surviving_child(engine, tenant_id: UUID) -> None:
    collection_id = uuid4()
    document_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": f"tenant-{tenant_id}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO collections (
                    id, tenant_id, name, retention_days, upload_max_bytes,
                    document_quota, storage_quota_bytes
                ) VALUES (:id, :tenant_id, :name, 0, 1000000, 10, 10000000)
                """
            ),
            {
                "id": collection_id,
                "tenant_id": tenant_id,
                "name": f"collection-{collection_id}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO source_documents (
                    id, tenant_id, collection_id, source_key, display_name
                ) VALUES (:id, :tenant_id, :collection_id, :key, 'test.pdf')
                """
            ),
            {
                "id": document_id,
                "tenant_id": tenant_id,
                "collection_id": collection_id,
                "key": f"source-{document_id}",
            },
        )
        base = {
            "tenant_id": tenant_id,
            "collection_id": collection_id,
            "document_id": document_id,
        }
        connection.execute(
            text(
                """
                INSERT INTO document_versions (
                    id, tenant_id, collection_id, document_id, version_number,
                    idempotency_key, content_sha256, size_bytes, object_key,
                    pipeline_profile, state, state_revision
                ) VALUES (
                    :id, :tenant_id, :collection_id, :document_id, 1,
                    :key, repeat('a', 64), 100, :object_key,
                    'integration', 'deleted', 1
                )
                """
            ),
            {
                **base,
                "id": parent_id,
                "key": f"parent-{parent_id}",
                "object_key": f"integration/parent-{parent_id}.pdf",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO document_versions (
                    id, tenant_id, collection_id, document_id, version_number,
                    idempotency_key, content_sha256, size_bytes, object_key,
                    pipeline_profile, state, reprocessed_from_version_id
                ) VALUES (
                    :id, :tenant_id, :collection_id, :document_id, 2,
                    :key, repeat('b', 64), 100, :object_key,
                    'integration', 'queued', :parent_id
                )
                """
            ),
            {
                **base,
                "id": child_id,
                "key": f"child-{child_id}",
                "object_key": f"integration/child-{child_id}.pdf",
                "parent_id": parent_id,
            },
        )


def _delete_tenant(engine, tenant_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "search_cleanup_entries",
            "object_cleanup_entries",
            "version_transition_events",
            "job_attempts",
            "index_publications",
            "chunk_embeddings",
            "chunks",
            "artifacts",
            "ingestion_jobs",
            "lifecycle_requests",
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
