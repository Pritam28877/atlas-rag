from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.router import router
from app.core.auth import Principal, require_principal
from app.core.request_limits import RequestBodyLimitMiddleware
from app.services.catalog.errors import CatalogNotFoundError, CatalogQuotaError

TENANT_ID = UUID("a7006ca9-bac4-4702-acaf-1e7c0dd6e7b6")
COLLECTION_ID = UUID("8c3dd611-c28f-4665-b680-bf47b07ee2f8")
DOCUMENT_ID = UUID("35ba671d-425f-46eb-946c-bb4501f7b5ad")
VERSION_ID = UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9")
JOB_ID = UUID("14391b0c-5f4c-4b02-9222-5308406cc2ad")
OPERATION_ID = UUID("6f4707cc-8b4f-4a21-8905-91e698a98dce")
NOW = datetime(2026, 7, 16, tzinfo=UTC)


class FakeCatalogService:
    def __init__(self) -> None:
        self.registration_calls: list[tuple[object, ...]] = []
        self.lifecycle_calls: list[tuple[object, ...]] = []
        self.not_found = False
        self.quota_exceeded = False

    def collection(self) -> dict[str, object]:
        return {
            "id": COLLECTION_ID,
            "name": "contracts",
            "retention_days": 30,
            "upload_max_bytes": 104857600,
            "document_quota": 1000,
            "storage_quota_bytes": 1073741824,
            "legal_hold": False,
            "created_at": NOW,
        }

    async def create_collection(self, principal, values):
        return self.collection()

    async def list_collections(self, principal, limit, cursor):
        assert principal.tenant_id == TENANT_ID
        assert limit <= 100
        return [self.collection()], "next-cursor"

    async def get_collection(self, principal, collection_id):
        if self.not_found:
            raise CatalogNotFoundError("collection not found")
        return self.collection()

    async def register_document(
        self, principal, collection_id, idempotency_key, values
    ):
        if self.quota_exceeded:
            raise CatalogQuotaError("collection storage quota exceeded")
        self.registration_calls.append(
            (principal, collection_id, idempotency_key, values)
        )
        return {
            "document_id": DOCUMENT_ID,
            "document_version_id": VERSION_ID,
            "state": "received",
            "upload": {
                "method": "PUT",
                "url": "https://storage.example.test/signed",
                "headers": {"content-type": "application/pdf"},
                "expires_in_seconds": 900,
            },
        }

    async def complete_upload(self, principal, version_id):
        return {
            "document_version_id": version_id,
            "job_id": JOB_ID,
            "state": "queued",
            "reason_code": None,
        }

    async def status(self, principal, version_id):
        return {
            "document_version_id": version_id,
            "document_id": DOCUMENT_ID,
            "collection_id": COLLECTION_ID,
            "state": "queued",
            "stage": "preflight",
            "progress_completed": 0,
            "progress_total": 0,
            "terminal_reason_code": None,
            "retry_eligible": False,
            "artifacts_available": True,
            "citations_ready": False,
            "lexical_index_ready": False,
            "vector_index_ready": False,
            "created_at": NOW,
            "updated_at": NOW,
        }

    async def lifecycle(
        self, principal, version_id, operation, idempotency_key, pipeline_profile
    ):
        self.lifecycle_calls.append(
            (
                principal,
                version_id,
                operation,
                idempotency_key,
                pipeline_profile,
            )
        )
        return {
            "operation_id": OPERATION_ID,
            "document_version_id": version_id,
            "operation": operation,
            "status": "pending",
            "version_state": "queued",
        }


def create_client(
    service: FakeCatalogService, *, authenticated: bool = True
) -> TestClient:
    application = FastAPI()
    application.add_middleware(RequestBodyLimitMiddleware, maximum_bytes=1024)
    application.state.catalog_service = service
    application.include_router(router, prefix="/v1")
    if authenticated:
        application.dependency_overrides[require_principal] = lambda: Principal(
            subject="user-123",
            tenant_id=TENANT_ID,
        )
    return TestClient(application)


def registration_body() -> dict[str, object]:
    return {
        "source_key": "contracts/nda.pdf",
        "display_name": "NDA.pdf",
        "content_sha256": "a" * 64,
        "size_bytes": 1024,
        "content_type": "application/pdf",
        "pipeline_profile": "pdf-v1",
    }


def test_catalog_requires_bearer_authentication() -> None:
    response = create_client(FakeCatalogService(), authenticated=False).get(
        "/v1/collections"
    )

    assert response.status_code == 401
    assert "token" not in response.text.lower()


def test_collection_list_is_bounded_and_cursor_paginated() -> None:
    response = create_client(FakeCatalogService()).get(
        "/v1/collections", params={"limit": 25}
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["next_cursor"] == "next-cursor"


def test_collection_list_rejects_unbounded_limit() -> None:
    response = create_client(FakeCatalogService()).get(
        "/v1/collections", params={"limit": 1000}
    )

    assert response.status_code == 422


def test_registration_requires_idempotency_key() -> None:
    service = FakeCatalogService()

    response = create_client(service).post(
        f"/v1/collections/{COLLECTION_ID}/documents",
        json=registration_body(),
    )

    assert response.status_code == 422
    assert service.registration_calls == []


def test_registration_returns_direct_upload_target_without_receiving_pdf() -> None:
    service = FakeCatalogService()

    response = create_client(service).post(
        f"/v1/collections/{COLLECTION_ID}/documents",
        headers={"Idempotency-Key": "request-0001"},
        json=registration_body(),
    )

    assert response.status_code == 201
    assert response.json()["upload"]["method"] == "PUT"
    assert response.json()["document_version_id"] == str(VERSION_ID)
    assert len(service.registration_calls) == 1


def test_registration_does_not_accept_pdf_request_body() -> None:
    service = FakeCatalogService()

    response = create_client(service).post(
        f"/v1/collections/{COLLECTION_ID}/documents",
        headers={
            "Idempotency-Key": "request-0001",
            "Content-Type": "application/pdf",
        },
        content=b"%PDF-large-payload",
    )

    assert response.status_code == 422
    assert service.registration_calls == []


def test_registration_rejects_oversized_body_before_parsing() -> None:
    service = FakeCatalogService()

    response = create_client(service).post(
        f"/v1/collections/{COLLECTION_ID}/documents",
        headers={
            "Idempotency-Key": "request-0001",
            "Content-Type": "application/json",
        },
        content=b"x" * 1025,
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}
    assert service.registration_calls == []


def test_registration_reports_safe_quota_error() -> None:
    service = FakeCatalogService()
    service.quota_exceeded = True

    response = create_client(service).post(
        f"/v1/collections/{COLLECTION_ID}/documents",
        headers={"Idempotency-Key": "request-0001"},
        json=registration_body(),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "QUOTA_EXCEEDED",
        "message": "collection storage quota exceeded",
    }


def test_complete_upload_returns_accepted_job_reference() -> None:
    response = create_client(FakeCatalogService()).post(
        f"/v1/document-versions/{VERSION_ID}/complete-upload"
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == str(JOB_ID)
    assert "object_key" not in response.text


def test_status_exposes_safe_progress_without_internal_content() -> None:
    response = create_client(FakeCatalogService()).get(
        f"/v1/document-versions/{VERSION_ID}"
    )

    assert response.status_code == 200
    assert response.json()["stage"] == "preflight"
    assert "object_key" not in response.text
    assert "exception" not in response.text.lower()


def test_lifecycle_request_is_idempotent_contract() -> None:
    service = FakeCatalogService()

    response = create_client(service).post(
        f"/v1/document-versions/{VERSION_ID}/lifecycle",
        headers={"Idempotency-Key": "delete-0001"},
        json={"operation": "delete"},
    )

    assert response.status_code == 202
    assert response.json()["operation_id"] == str(OPERATION_ID)
    assert response.json()["status"] == "pending"
    assert len(service.lifecycle_calls) == 1


def test_reprocess_requires_explicit_new_pipeline_profile() -> None:
    client = create_client(FakeCatalogService())

    missing = client.post(
        f"/v1/document-versions/{VERSION_ID}/lifecycle",
        headers={"Idempotency-Key": "reprocess-missing-profile"},
        json={"operation": "reprocess"},
    )
    valid = client.post(
        f"/v1/document-versions/{VERSION_ID}/lifecycle",
        headers={"Idempotency-Key": "reprocess-profile-v2"},
        json={"operation": "reprocess", "pipeline_profile": "pdf-v2"},
    )

    assert missing.status_code == 422
    assert valid.status_code == 202


def test_cross_scope_resource_is_reported_as_not_found() -> None:
    service = FakeCatalogService()
    service.not_found = True

    response = create_client(service).get(f"/v1/collections/{COLLECTION_ID}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"
