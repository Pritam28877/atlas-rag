import os
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.auth import Principal, require_principal
from app.core.config import get_settings
from app.core.database import normalize_database_url
from app.core.storage import sha256_hex
from app.main import create_app


def required_integration_environment() -> dict[str, str]:
    names = (
        "TEST_DATABASE_URL",
        "P2_STORAGE_ENDPOINT_URL",
        "P2_STORAGE_BUCKET_NAME",
        "P2_STORAGE_ACCESS_KEY_ID",
        "P2_STORAGE_SECRET_ACCESS_KEY",
        "P2_STORAGE_USE_TLS",
        "P2_BROKER_URL",
        "P2_BROKER_USE_TLS",
    )
    values = {name: os.environ.get(name, "") for name in names}
    if any(not value for value in values.values()):
        pytest.skip("P4 catalog integration infrastructure is not configured")
    return values


@pytest.mark.catalog_integration
def test_real_direct_upload_and_catalog_intake(monkeypatch) -> None:
    environment = required_integration_environment()
    database_url = environment["TEST_DATABASE_URL"]
    parsed_url = make_url(database_url)
    if parsed_url.database is None or not parsed_url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")

    tenant_id = uuid4()
    other_tenant_id = uuid4()
    subject = f"integration-{uuid4()}"
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    config = Config("alembic.ini")
    monkeypatch.setenv("DATABASE__URL", database_url)
    monkeypatch.setenv("STORAGE__ENDPOINT_URL", environment["P2_STORAGE_ENDPOINT_URL"])
    monkeypatch.setenv("STORAGE__BUCKET_NAME", environment["P2_STORAGE_BUCKET_NAME"])
    monkeypatch.setenv(
        "STORAGE__ACCESS_KEY_ID", environment["P2_STORAGE_ACCESS_KEY_ID"]
    )
    monkeypatch.setenv(
        "STORAGE__SECRET_ACCESS_KEY", environment["P2_STORAGE_SECRET_ACCESS_KEY"]
    )
    monkeypatch.setenv("STORAGE__USE_TLS", environment["P2_STORAGE_USE_TLS"])
    monkeypatch.setenv("BROKER__URL", environment["P2_BROKER_URL"])
    monkeypatch.setenv("BROKER__USE_TLS", environment["P2_BROKER_USE_TLS"])
    get_settings.cache_clear()
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": f"integration-{tenant_id}"},
        )

    active_principal = {"value": Principal(subject=subject, tenant_id=tenant_id)}
    application = create_app()
    application.dependency_overrides[require_principal] = lambda: active_principal[
        "value"
    ]
    pdf = b"%PDF-1.7\n%%EOF\n"
    try:
        with TestClient(application) as client:
            collection = client.post(
                "/v1/collections",
                json={
                    "name": "integration-documents",
                    "retention_days": 0,
                    "document_quota": 10,
                    "storage_quota_bytes": 1048576,
                },
            )
            assert collection.status_code == 201, collection.text
            collection_id = collection.json()["id"]
            registration_body = {
                "source_key": "fixtures/smoke.pdf",
                "display_name": "smoke.pdf",
                "content_sha256": sha256_hex(pdf),
                "size_bytes": len(pdf),
                "content_type": "application/pdf",
                "pipeline_profile": "pdf-v1",
            }
            registration = client.post(
                f"/v1/collections/{collection_id}/documents",
                headers={"Idempotency-Key": "integration-registration-1"},
                json=registration_body,
            )
            assert registration.status_code == 201, registration.text
            repeated = client.post(
                f"/v1/collections/{collection_id}/documents",
                headers={"Idempotency-Key": "integration-registration-1"},
                json=registration_body,
            )
            assert repeated.json()["document_version_id"] == registration.json()[
                "document_version_id"
            ]
            upload = registration.json()["upload"]
            uploaded = httpx.put(
                upload["url"],
                headers=upload["headers"],
                content=pdf,
                timeout=30,
            )
            assert uploaded.is_success, uploaded.text
            version_id = registration.json()["document_version_id"]
            completed = client.post(
                f"/v1/document-versions/{version_id}/complete-upload"
            )
            assert completed.status_code == 202, completed.text
            assert completed.json()["state"] == "queued"
            status_response = client.get(f"/v1/document-versions/{version_id}")
            assert status_response.status_code == 200
            assert status_response.json()["stage"] == "preflight"

            active_principal["value"] = Principal(
                subject=subject, tenant_id=other_tenant_id
            )
            forbidden = client.get(f"/v1/document-versions/{version_id}")
            assert forbidden.status_code == 404
    finally:
        get_settings.cache_clear()
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
                text("DELETE FROM tenants WHERE id = :tenant_id"), parameters
            )
        engine.dispose()
