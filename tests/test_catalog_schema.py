import re
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def render_catalog_upgrade_sql(monkeypatch) -> str:
    output = StringIO()
    config = Config(PROJECT_ROOT / "alembic.ini", output_buffer=output)
    monkeypatch.setenv(
        "DATABASE__URL",
        "postgresql://catalog_test:catalog_test@localhost:5432/catalog_test",
    )
    get_settings.cache_clear()
    try:
        command.upgrade(config, "head", sql=True)
    finally:
        get_settings.cache_clear()
    return output.getvalue()


def test_catalog_migration_defines_complete_tenant_scoped_model(monkeypatch) -> None:
    sql = render_catalog_upgrade_sql(monkeypatch)
    normalized_sql = re.sub(r"\s+", " ", sql)

    expected_tables = {
        "tenants",
        "collections",
        "collection_memberships",
        "source_documents",
        "document_versions",
        "artifacts",
        "ingestion_jobs",
        "job_attempts",
        "version_transition_events",
        "chunks",
        "chunk_embeddings",
        "index_publications",
        "lifecycle_requests",
    }
    for table_name in expected_tables:
        assert f"CREATE TABLE {table_name}" in sql

    assert sql.count("FOREIGN KEY (tenant_id, collection_id") >= 10
    assert sql.count("tenant_id, collection_id, document_version_id, chunk_id") >= 2
    assert "UNIQUE (tenant_id, collection_id, idempotency_key)" in normalized_sql
    assert "UNIQUE (tenant_id, collection_id, job_id, attempt_number)" in normalized_sql
    assert (
        "UNIQUE ( tenant_id, collection_id, document_version_id, to_revision )"
        in normalized_sql
    )


def test_catalog_migration_keeps_large_content_out_of_relational_columns(
    monkeypatch,
) -> None:
    sql = render_catalog_upgrade_sql(monkeypatch)

    assert "object_key VARCHAR" in sql
    assert "content_object_key VARCHAR" in sql
    assert "vector_object_key VARCHAR" in sql
    assert " BYTEA" not in sql
    assert " VECTOR(" not in sql


def test_catalog_migration_enforces_immutable_provenance(monkeypatch) -> None:
    sql = render_catalog_upgrade_sql(monkeypatch)

    assert "CREATE TRIGGER artifacts_immutable" in sql
    assert "CREATE TRIGGER document_versions_identity_immutable" in sql
    assert "CREATE TRIGGER document_versions_ready_publication" in sql
    assert "ready state requires complete publication evidence" in sql
    assert "UNIQUE (tenant_id, collection_id, id)" in sql
    assert "content_sha256 ~ '^[0-9a-f]{64}$'" in sql
