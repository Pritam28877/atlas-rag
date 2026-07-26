import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.database import normalize_database_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_migration_graph_has_one_head_and_reversible_chain() -> None:
    config = Config(PROJECT_ROOT / "alembic.ini")
    migrations = ScriptDirectory.from_config(config)

    assert migrations.get_heads() == ["20260727_09"]
    health_guard = migrations.get_revision("20260727_09")
    assert health_guard is not None
    assert health_guard.down_revision == "20260727_08"
    health = migrations.get_revision("20260727_08")
    assert health is not None
    assert health.down_revision == "20260726_07"
    projections = migrations.get_revision("20260726_07")
    assert projections is not None
    assert projections.down_revision == "20260726_06"
    journal = migrations.get_revision("20260726_06")
    assert journal is not None
    assert journal.down_revision == "20260717_05"
    guards = migrations.get_revision("20260717_05")
    assert guards is not None
    assert guards.down_revision == "20260717_04"
    lifecycle = migrations.get_revision("20260717_04")
    assert lifecycle is not None
    assert lifecycle.down_revision == "20260716_03"
    processing = migrations.get_revision("20260716_03")
    assert processing is not None
    assert processing.down_revision == "20260716_02"
    catalog = migrations.get_revision("20260716_02")
    assert catalog is not None
    assert catalog.down_revision == "20260713_01"
    baseline = migrations.get_revision("20260713_01")
    assert baseline is not None
    assert baseline.down_revision is None


def test_migration_paths_resolve_from_project_root() -> None:
    config = Config(PROJECT_ROOT / "alembic.ini")
    migrations = ScriptDirectory.from_config(config)

    assert Path(migrations.dir).resolve() == PROJECT_ROOT / "migrations"


@pytest.mark.database_integration
def test_clean_postgres_migrates_forward_and_rolls_back(monkeypatch) -> None:
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if test_database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    parsed_url = make_url(test_database_url)
    if parsed_url.database is None or not parsed_url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")

    normalized_url = normalize_database_url(test_database_url)
    engine = create_engine(normalized_url, pool_pre_ping=True)
    config = Config(PROJECT_ROOT / "alembic.ini")
    monkeypatch.setenv("DATABASE__URL", test_database_url)
    get_settings.cache_clear()

    try:
        existing_tables = inspect(engine).get_table_names(schema="public")
        assert existing_tables == []

        command.upgrade(config, "head")
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "20260727_09"

        command.downgrade(config, "base")
        with engine.connect() as connection:
            remaining_versions = connection.execute(
                text("SELECT count(*) FROM alembic_version")
            ).scalar_one()
        assert remaining_versions == 0
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        get_settings.cache_clear()
        engine.dispose()


@pytest.mark.database_integration
def test_revision_five_reconciles_populated_revision_four(monkeypatch) -> None:
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if test_database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    parsed_url = make_url(test_database_url)
    if parsed_url.database is None or not parsed_url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")

    normalized_url = normalize_database_url(test_database_url)
    engine = create_engine(normalized_url, pool_pre_ping=True)
    config = Config(PROJECT_ROOT / "alembic.ini")
    monkeypatch.setenv("DATABASE__URL", test_database_url)
    get_settings.cache_clear()
    try:
        if "alembic_version" in inspect(engine).get_table_names(schema="public"):
            command.downgrade(config, "base")
        command.upgrade(config, "20260717_04")
        with engine.begin() as connection:
            connection.execute(text(_POPULATED_REVISION_FOUR_FIXTURE))

        command.upgrade(config, "20260717_05")
        with engine.connect() as connection:
            active_delete_count = connection.scalar(
                text(
                    """
                    SELECT count(*) FROM ingestion_jobs
                    WHERE stage = 'delete' AND state IN (
                        'pending', 'leased', 'running', 'retry_scheduled'
                    )
                    """
                )
            )
            request_state = connection.scalar(
                text(
                    """
                    SELECT state FROM lifecycle_requests
                    WHERE id = '10000000-0000-0000-0000-000000000009'
                    """
                )
            )
            cleanup_target = connection.execute(
                text(
                    """
                    SELECT target_name, target_version
                    FROM search_cleanup_entries
                    """
                )
            ).one()
            active_target = connection.execute(
                text(
                    """
                    SELECT DISTINCT target_name, target_version
                    FROM index_publications WHERE deleted_at IS NULL
                    """
                )
            ).one()
            manifest_target = connection.execute(
                text(
                    """
                    SELECT search_target_name, search_target_version
                    FROM artifacts
                    WHERE generator_name = 'opensearch'
                      AND search_target_name IS NOT NULL
                    """
                )
            ).one()

        assert active_delete_count == 1
        assert request_state == "pending"
        assert cleanup_target == ("index-a", "target-v1")
        assert active_target == ("index-b", "target-v2")
        assert manifest_target == active_target
    finally:
        if "alembic_version" in inspect(engine).get_table_names(schema="public"):
            command.downgrade(config, "base")
        get_settings.cache_clear()
        engine.dispose()


_POPULATED_REVISION_FOUR_FIXTURE = """
INSERT INTO tenants (id, name)
VALUES ('10000000-0000-0000-0000-000000000001', 'migration-tenant');
INSERT INTO collections (
    id, tenant_id, name, upload_max_bytes, document_quota,
    storage_quota_bytes
) VALUES (
    '10000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000001',
    'migration-collection', 1000000, 100, 10000000
);
INSERT INTO source_documents (
    id, tenant_id, collection_id, source_key, display_name
) VALUES (
    '10000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000002',
    'migration-source', 'migration.pdf'
);
INSERT INTO document_versions (
    id, tenant_id, collection_id, document_id, version_number,
    idempotency_key, content_sha256, size_bytes, object_key,
    pipeline_profile, state
) VALUES (
    '10000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000003', 1,
    'migration-version', repeat('a', 64), 100, 'migration/original.pdf',
    'migration-profile', 'indexing'
);
INSERT INTO artifacts (
    id, tenant_id, collection_id, document_version_id, artifact_type,
    object_key, generator_name, generator_version, checksum_sha256, size_bytes
) VALUES
    ('10000000-0000-0000-0000-000000000005',
     '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004', 'chunk_manifest',
     'migration/chunks', 'chunker', 'v1', repeat('b', 64), 10),
    ('10000000-0000-0000-0000-000000000006',
     '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004', 'index_manifest',
     'migration/index-a', 'opensearch', 'target-v1', repeat('c', 64), 10),
    ('10000000-0000-0000-0000-000000000007',
     '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004', 'index_manifest',
     'migration/index-b', 'opensearch', 'target-v2', repeat('d', 64), 10);
INSERT INTO chunks (
    id, tenant_id, collection_id, document_version_id, source_artifact_id,
    ordinal, page_start, page_end, content_sha256, content_object_key,
    token_count, chunker_name, chunker_version
) VALUES (
    '10000000-0000-0000-0000-000000000008',
    '10000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000005', 0, 1, 1,
    repeat('e', 64), 'migration/chunks', 1, 'chunker', 'v1'
);
INSERT INTO chunk_embeddings (
    id, tenant_id, collection_id, document_version_id, chunk_id,
    provider, model_name, model_version, dimensions,
    vector_object_key, vector_checksum_sha256
) VALUES (
    '10000000-0000-0000-0000-00000000000a',
    '10000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000008',
    'provider', 'model', 'v1', 2, 'migration/vectors', repeat('f', 64)
);
INSERT INTO index_publications (
    id, tenant_id, collection_id, document_version_id, chunk_id,
    publication_kind, target_name, target_version,
    external_record_id, published_at
) VALUES
    (gen_random_uuid(), '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004',
     '10000000-0000-0000-0000-000000000008', 'lexical',
     'index-a', 'target-v1', 'a-lexical', now() - interval '2 seconds'),
    (gen_random_uuid(), '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004',
     '10000000-0000-0000-0000-000000000008', 'vector',
     'index-a', 'target-v1', 'a-vector', now() - interval '2 seconds'),
    (gen_random_uuid(), '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004',
     '10000000-0000-0000-0000-000000000008', 'lexical',
     'index-b', 'target-v2', 'b-lexical', now() - interval '1 second'),
    (gen_random_uuid(), '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004',
     '10000000-0000-0000-0000-000000000008', 'vector',
     'index-b', 'target-v2', 'b-vector', now() - interval '1 second');
UPDATE document_versions SET state = 'ready', state_revision = 1
WHERE id = '10000000-0000-0000-0000-000000000004';
INSERT INTO lifecycle_requests (
    id, tenant_id, collection_id, document_version_id,
    source_document_version_id, request_type, idempotency_key, requested_by
) VALUES (
    '10000000-0000-0000-0000-000000000009',
    '10000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000004',
    'delete', 'migration-delete', 'migration-test'
);
INSERT INTO ingestion_jobs (
    id, tenant_id, collection_id, document_version_id, stage, generation,
    max_attempts, lifecycle_request_id, created_at
) VALUES
    ('10000000-0000-0000-0000-00000000000b',
     '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004', 'delete', 0, 3,
     '10000000-0000-0000-0000-000000000009', now() - interval '2 seconds'),
    ('10000000-0000-0000-0000-00000000000c',
     '10000000-0000-0000-0000-000000000001',
     '10000000-0000-0000-0000-000000000002',
     '10000000-0000-0000-0000-000000000004', 'delete', 1, 3,
     '10000000-0000-0000-0000-000000000009', now() - interval '1 second');
"""
