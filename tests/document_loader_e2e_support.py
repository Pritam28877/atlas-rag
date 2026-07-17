"""Small database/API helpers for the opt-in document-loader system test."""

import contextlib
import os
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.api.v1.catalog import get_catalog_service
from app.core.auth import Principal, require_principal
from app.core.config import Settings, get_settings
from app.core.storage import sha256_hex
from app.main import create_app
from app.services.catalog.service import CatalogService


def required_environment(
    *,
    require_broker: bool = False,
    require_model: bool = True,
) -> dict[str, str]:
    names = [
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
    ]
    if require_broker:
        names.extend(("P2_BROKER_URL", "P2_BROKER_USE_TLS"))
    if require_model:
        names.append("P2_EMBEDDING_MODEL_DIRECTORY")
    values = {name: os.environ.get(name, "") for name in names}
    if any(not value for value in values.values()):
        pytest.skip("document loader end-to-end infrastructure is not configured")
    database_url = values["TEST_DATABASE_URL"]
    if not database_url.rsplit("/", 1)[-1].endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")
    return values


def configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    index_name: str,
    temporary_directory: Path,
) -> None:
    mappings = {
        "DATABASE__URL": values["TEST_DATABASE_URL"],
        "STORAGE__ENDPOINT_URL": values["P2_STORAGE_ENDPOINT_URL"],
        "STORAGE__BUCKET_NAME": values["P2_STORAGE_BUCKET_NAME"],
        "STORAGE__ACCESS_KEY_ID": values["P2_STORAGE_ACCESS_KEY_ID"],
        "STORAGE__SECRET_ACCESS_KEY": values["P2_STORAGE_SECRET_ACCESS_KEY"],
        "STORAGE__USE_TLS": values["P2_STORAGE_USE_TLS"],
        "STORAGE__SERVER_SIDE_ENCRYPTION": values[
            "P2_STORAGE_SERVER_SIDE_ENCRYPTION"
        ],
        "SEARCH__ENDPOINT_URL": values["P2_SEARCH_ENDPOINT_URL"],
        "SEARCH__USERNAME": values["P2_SEARCH_USERNAME"],
        "SEARCH__PASSWORD": values["P2_SEARCH_PASSWORD"],
        "SEARCH__VERIFY_TLS": values["P2_SEARCH_VERIFY_TLS"],
        "SEARCH__INDEX_NAME": index_name,
        "EMBEDDING__BATCH_SIZE": "2",
        "WORKERS__JOB_TEMP_DIRECTORY": str(temporary_directory),
    }
    optional_mappings = {
        "BROKER__URL": values.get("P2_BROKER_URL"),
        "BROKER__USE_TLS": values.get("P2_BROKER_USE_TLS"),
        "EMBEDDING__MODEL_DIRECTORY": values.get(
            "P2_EMBEDDING_MODEL_DIRECTORY"
        ),
    }
    mappings.update(
        {name: value for name, value in optional_mappings.items() if value}
    )
    for name, value in mappings.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def submit_pdf(
    tenant_id: UUID,
    fixture_path: Path,
    *,
    catalog_service: CatalogService | None = None,
    name: str = "document-loader-e2e",
) -> tuple[UUID, UUID, UUID]:
    application = create_app()
    application.dependency_overrides[require_principal] = lambda: Principal(
        subject="document-loader-e2e",
        tenant_id=tenant_id,
    )
    if catalog_service is not None:
        application.dependency_overrides[get_catalog_service] = lambda: catalog_service
    pdf = fixture_path.read_bytes()
    with TestClient(application) as client:
        collection = client.post(
            "/v1/collections",
            json={
                "name": name,
                "retention_days": 0,
                "document_quota": 10,
                "storage_quota_bytes": 1048576,
            },
        )
        assert collection.status_code == 201, collection.text
        collection_id = UUID(collection.json()["id"])
        registration = client.post(
            f"/v1/collections/{collection_id}/documents",
            headers={"Idempotency-Key": f"{name}-registration"},
            json={
                "source_key": f"fixtures/{fixture_path.name}",
                "display_name": fixture_path.name,
                "content_sha256": sha256_hex(pdf),
                "size_bytes": len(pdf),
                "content_type": "application/pdf",
                "pipeline_profile": "pdf-v1",
            },
        )
        assert registration.status_code == 201, registration.text
        upload = registration.json()["upload"]
        upload_response = httpx.put(
            upload["url"], headers=upload["headers"], content=pdf, timeout=30
        )
        assert upload_response.is_success, upload_response.text
        version_id = UUID(registration.json()["document_version_id"])
        completion = client.post(
            f"/v1/document-versions/{version_id}/complete-upload"
        )
        assert completion.status_code == 202, completion.text
        return version_id, UUID(completion.json()["job_id"]), collection_id


def request_lifecycle(
    tenant_id: UUID,
    version_id: UUID,
    operation: str,
    idempotency_key: str,
    catalog_service: CatalogService | None = None,
    pipeline_profile: str | None = None,
) -> UUID:
    response = post_lifecycle(
        tenant_id,
        version_id,
        operation,
        idempotency_key,
        catalog_service,
        pipeline_profile,
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["document_version_id"])


def post_lifecycle(
    tenant_id: UUID,
    version_id: UUID,
    operation: str,
    idempotency_key: str,
    catalog_service: CatalogService | None = None,
    pipeline_profile: str | None = None,
) -> httpx.Response:
    application = create_app()
    application.dependency_overrides[require_principal] = lambda: Principal(
        subject="document-loader-e2e",
        tenant_id=tenant_id,
    )
    if catalog_service is not None:
        application.dependency_overrides[get_catalog_service] = lambda: catalog_service
    body = {"operation": operation}
    if pipeline_profile is not None:
        body["pipeline_profile"] = pipeline_profile
    with TestClient(application) as client:
        response = client.post(
            f"/v1/document-versions/{version_id}/lifecycle",
            headers={"Idempotency-Key": idempotency_key},
            json=body,
        )
    return response


def job_id(engine: Engine, version_id: UUID, stage: str) -> UUID:
    with engine.connect() as connection:
        value = connection.scalar(
            text(
                """
                SELECT id FROM ingestion_jobs
                WHERE document_version_id = :version_id AND stage = :stage
                ORDER BY generation DESC LIMIT 1
                """
            ),
            {"version_id": version_id, "stage": stage},
        )
    assert isinstance(value, UUID)
    return value


def assert_version_states(
    engine: Engine, source_id: UUID, replacement_id: UUID
) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, state FROM document_versions
                WHERE id IN (:source_id, :replacement_id)
                """
            ),
            {"source_id": source_id, "replacement_id": replacement_id},
        )
        states = {UUID(str(row.id)): str(row.state) for row in rows}
    assert states == {source_id: "superseded", replacement_id: "ready"}


def wait_for_search(settings: Settings) -> None:
    assert settings.search.endpoint_url
    assert settings.search.username and settings.search.password
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            with httpx.Client(
                auth=(
                    settings.search.username,
                    settings.search.password.get_secret_value(),
                ),
                verify=settings.search.verify_tls,
                timeout=2,
            ) as client:
                response = client.get(
                    f"{settings.search.endpoint_url}/_cluster/health"
                )
            if response.is_success:
                return
        time.sleep(0.25)
    raise RuntimeError("OpenSearch did not become ready")


def assert_authorized_search(
    settings: Settings,
    tenant_id: UUID,
    version_id: UUID,
) -> None:
    filters = [
        {"term": {"tenant_id": str(tenant_id)}},
        {"term": {"document_version_id": str(version_id)}},
        {"term": {"publication_complete": True}},
    ]
    with _search_client(settings) as client:
        allowed = client.post(
            f"/{settings.search.index_name}/_search",
            json={"query": {"bool": {"filter": filters}}},
        )
        filters[0] = {"term": {"tenant_id": str(uuid4())}}
        denied = client.post(
            f"/{settings.search.index_name}/_search",
            json={"query": {"bool": {"filter": filters}}},
        )
    assert allowed.is_success and allowed.json()["hits"]["total"]["value"] > 0
    assert denied.is_success and denied.json()["hits"]["total"]["value"] == 0


def search_hit_count(
    settings: Settings,
    tenant_id: UUID,
    version_id: UUID,
) -> int:
    with _search_client(settings) as client:
        response = client.post(
            f"/{settings.search.index_name}/_search",
            json={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": str(tenant_id)}},
                            {"term": {"document_version_id": str(version_id)}},
                            {"term": {"publication_complete": True}},
                        ]
                    }
                }
            },
        )
    assert response.is_success, response.text
    return int(response.json()["hits"]["total"]["value"])


def delete_index(settings: Settings) -> None:
    if not settings.search.endpoint_url:
        return
    with contextlib.suppress(httpx.HTTPError):
        with _search_client(settings) as client:
            client.delete(f"/{settings.search.index_name}")


def delete_tenant(engine: Engine, tenant_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "version_transition_events",
            "job_attempts",
            "object_cleanup_entries",
            "ingestion_jobs",
            "lifecycle_requests",
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


def _search_client(settings: Settings) -> httpx.Client:
    assert settings.search.endpoint_url
    assert settings.search.username and settings.search.password
    return httpx.Client(
        base_url=settings.search.endpoint_url,
        auth=(
            settings.search.username,
            settings.search.password.get_secret_value(),
        ),
        verify=settings.search.verify_tls,
        timeout=30,
    )
