import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.v1.catalog import get_catalog_service
from app.core.auth import Principal, require_principal
from app.core.config import get_settings
from app.core.database import Database, normalize_database_url
from app.core.storage import ObjectStorage, sha256_hex
from app.main import create_app
from app.services.catalog.service import CatalogService
from app.services.ingestion.ocr_service import OcrIngestionService
from app.services.ingestion.service import NativeIngestionService
from app.workers.celery_app import IngestionJobPayload, PipelineTask
from tests.native_ingestion_integration_support import (
    RecordingDispatcher,
    required_environment,
)
from tests.native_ingestion_integration_support import (
    configure_environment as _configure_environment,
)
from tests.native_ingestion_integration_support import (
    delete_tenant as _delete_tenant,
)
from tests.native_ingestion_integration_support import (
    simulate_abandoned_claim as _simulate_abandoned_claim,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATIVE_PDF = PROJECT_ROOT / "benchmarks" / "fixtures" / "native-simple-en-001.pdf"
MIXED_PDF = PROJECT_ROOT / "benchmarks" / "fixtures" / "mixed-en-001.pdf"
ENCRYPTED_PDF = PROJECT_ROOT / "benchmarks" / "fixtures" / "encrypted-001.pdf"


@pytest.mark.native_integration
@pytest.mark.ocr_integration
def test_native_worker_publishes_normalized_artifacts_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment = required_environment()
    database_url = environment["TEST_DATABASE_URL"]
    if not database_url.rsplit("/", 1)[-1].endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")
    _configure_environment(monkeypatch, environment, tmp_path)
    tenant_id = uuid4()
    subject = f"native-integration-{uuid4()}"
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": f"native-{tenant_id}"},
        )
    application = create_app()
    application.dependency_overrides[require_principal] = lambda: Principal(
        subject=subject,
        tenant_id=tenant_id,
    )
    intake_dispatcher = RecordingDispatcher()
    application.dependency_overrides[get_catalog_service] = lambda: CatalogService(
        application.state.database,
        application.state.storage,
        intake_dispatcher,
        application.state.settings,
    )
    pdf = NATIVE_PDF.read_bytes()
    mixed_pdf = MIXED_PDF.read_bytes()
    encrypted_pdf = ENCRYPTED_PDF.read_bytes()
    try:
        with TestClient(application) as client:
            collection_response = client.post(
                "/v1/collections",
                json={
                    "name": "native-worker-integration",
                    "retention_days": 0,
                    "document_quota": 10,
                    "storage_quota_bytes": 1048576,
                },
            )
            assert collection_response.status_code == 201
            collection_id = collection_response.json()["id"]
            registration = client.post(
                f"/v1/collections/{collection_id}/documents",
                headers={"Idempotency-Key": "native-worker-integration-1"},
                json={
                    "source_key": "fixtures/native-simple.pdf",
                    "display_name": "native-simple.pdf",
                    "content_sha256": sha256_hex(pdf),
                    "size_bytes": len(pdf),
                    "content_type": "application/pdf",
                    "pipeline_profile": "pdf-v1",
                },
            )
            assert registration.status_code == 201, registration.text
            upload = registration.json()["upload"]
            upload_response = httpx.put(
                upload["url"],
                headers=upload["headers"],
                content=pdf,
                timeout=30,
            )
            assert upload_response.is_success, upload_response.text
            version_id = UUID(registration.json()["document_version_id"])
            completion = client.post(
                f"/v1/document-versions/{version_id}/complete-upload"
            )
            assert completion.status_code == 202, completion.text
            job_id = UUID(completion.json()["job_id"])
            mixed_registration = client.post(
                f"/v1/collections/{collection_id}/documents",
                headers={"Idempotency-Key": "mixed-worker-integration-1"},
                json={
                    "source_key": "fixtures/mixed.pdf",
                    "display_name": "mixed.pdf",
                    "content_sha256": sha256_hex(mixed_pdf),
                    "size_bytes": len(mixed_pdf),
                    "content_type": "application/pdf",
                    "pipeline_profile": "pdf-v1",
                },
            )
            assert mixed_registration.status_code == 201, mixed_registration.text
            mixed_upload = mixed_registration.json()["upload"]
            mixed_upload_response = httpx.put(
                mixed_upload["url"],
                headers=mixed_upload["headers"],
                content=mixed_pdf,
                timeout=30,
            )
            assert mixed_upload_response.is_success, mixed_upload_response.text
            mixed_version_id = UUID(
                mixed_registration.json()["document_version_id"]
            )
            mixed_completion = client.post(
                f"/v1/document-versions/{mixed_version_id}/complete-upload"
            )
            assert mixed_completion.status_code == 202, mixed_completion.text
            mixed_job_id = UUID(mixed_completion.json()["job_id"])
            encrypted_registration = client.post(
                f"/v1/collections/{collection_id}/documents",
                headers={"Idempotency-Key": "encrypted-worker-integration-1"},
                json={
                    "source_key": "fixtures/encrypted.pdf",
                    "display_name": "encrypted.pdf",
                    "content_sha256": sha256_hex(encrypted_pdf),
                    "size_bytes": len(encrypted_pdf),
                    "content_type": "application/pdf",
                    "pipeline_profile": "pdf-v1",
                },
            )
            assert encrypted_registration.status_code == 201
            encrypted_upload = encrypted_registration.json()["upload"]
            encrypted_upload_response = httpx.put(
                encrypted_upload["url"],
                headers=encrypted_upload["headers"],
                content=encrypted_pdf,
                timeout=30,
            )
            assert encrypted_upload_response.is_success
            encrypted_version_id = UUID(
                encrypted_registration.json()["document_version_id"]
            )
            encrypted_completion = client.post(
                f"/v1/document-versions/{encrypted_version_id}/complete-upload"
            )
            assert encrypted_completion.status_code == 202
            encrypted_job_id = UUID(encrypted_completion.json()["job_id"])

        settings = get_settings()
        database = Database(settings.database)
        storage = ObjectStorage(
            settings.storage,
            provider_timeouts=settings.provider_timeouts,
        )
        dispatcher = RecordingDispatcher()
        service = NativeIngestionService(
            database,
            storage,
            settings,
            dispatcher,
        )
        payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=version_id,
            job_id=job_id,
        )
        mixed_payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=mixed_version_id,
            job_id=mixed_job_id,
        )
        encrypted_payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=encrypted_version_id,
            job_id=encrypted_job_id,
        )
        try:
            asyncio.run(_simulate_abandoned_claim(database, payload))
            asyncio.run(service.process(payload, "integration-native-worker"))
            asyncio.run(service.process(payload, "duplicate-delivery"))
            asyncio.run(service.process(mixed_payload, "integration-native-worker"))
            asyncio.run(
                service.process(encrypted_payload, "integration-native-worker")
            )
            ocr_payload = IngestionJobPayload(
                tenant_id=tenant_id,
                document_version_id=mixed_version_id,
                job_id=next(
                    call[3]
                    for call in dispatcher.calls
                    if call[0] is PipelineTask.OCR_PROCESS
                ),
            )
            ocr_service = OcrIngestionService(
                database, storage, settings, dispatcher
            )
            asyncio.run(ocr_service.process(ocr_payload, "integration-ocr-worker"))
        finally:
            asyncio.run(database.close())
            storage.close()

        with engine.connect() as connection:
            version = connection.execute(
                text(
                    """
                    SELECT state, page_count FROM document_versions
                    WHERE tenant_id = :tenant_id AND id = :version_id
                    """
                ),
                {"tenant_id": tenant_id, "version_id": version_id},
            ).mappings().one()
            artifacts = connection.execute(
                text(
                    """
                    SELECT artifact_type, count(*) AS count
                    FROM artifacts
                    WHERE tenant_id = :tenant_id AND document_version_id = :version_id
                    GROUP BY artifact_type
                    """
                ),
                {"tenant_id": tenant_id, "version_id": version_id},
            ).mappings().all()
            jobs = connection.execute(
                text(
                    """
                    SELECT stage, state, max(attempt_count) AS attempt_count,
                           count(*) AS count FROM ingestion_jobs
                    WHERE tenant_id = :tenant_id AND document_version_id = :version_id
                    GROUP BY stage, state
                    """
                ),
                {"tenant_id": tenant_id, "version_id": version_id},
            ).mappings().all()
            mixed_version = connection.execute(
                text(
                    """
                    SELECT state, page_count FROM document_versions
                    WHERE tenant_id = :tenant_id AND id = :version_id
                    """
                ),
                {"tenant_id": tenant_id, "version_id": mixed_version_id},
            ).mappings().one()
            mixed_artifacts = connection.execute(
                text(
                    """
                    SELECT artifact_type, count(*) AS count
                    FROM artifacts
                    WHERE tenant_id = :tenant_id AND document_version_id = :version_id
                    GROUP BY artifact_type
                    """
                ),
                {"tenant_id": tenant_id, "version_id": mixed_version_id},
            ).mappings().all()
            mixed_jobs = connection.execute(
                text(
                    """
                    SELECT stage, state, count(*) AS count FROM ingestion_jobs
                    WHERE tenant_id = :tenant_id AND document_version_id = :version_id
                    GROUP BY stage, state
                    """
                ),
                {"tenant_id": tenant_id, "version_id": mixed_version_id},
            ).mappings().all()
            attempt_outcomes = connection.execute(
                text(
                    """
                    SELECT attempt_number, outcome FROM job_attempts
                    WHERE tenant_id = :tenant_id AND job_id = :job_id
                    ORDER BY attempt_number
                    """
                ),
                {"tenant_id": tenant_id, "job_id": job_id},
            ).all()
            encrypted_version = connection.execute(
                text(
                    """
                    SELECT state, terminal_reason_code FROM document_versions
                    WHERE tenant_id = :tenant_id AND id = :version_id
                    """
                ),
                {"tenant_id": tenant_id, "version_id": encrypted_version_id},
            ).mappings().one()
            encrypted_artifacts = connection.execute(
                text(
                    """
                    SELECT artifact_type FROM artifacts
                    WHERE tenant_id = :tenant_id AND document_version_id = :version_id
                    ORDER BY artifact_type
                    """
                ),
                {"tenant_id": tenant_id, "version_id": encrypted_version_id},
            ).scalars().all()
        assert dict(version) == {"state": "chunking", "page_count": 1}
        assert {row["artifact_type"]: row["count"] for row in artifacts} == {
            "original_pdf": 1,
            "inspection_report": 1,
            "extracted_pages": 1,
            "normalized_document": 1,
        }
        assert {
            (row["stage"], row["state"], row["attempt_count"], row["count"])
            for row in jobs
        } == {
            ("preflight", "succeeded", 2, 1),
            ("chunk", "pending", 0, 1),
        }
        assert dict(mixed_version) == {"state": "chunking", "page_count": 2}
        assert {row["artifact_type"]: row["count"] for row in mixed_artifacts} == {
            "original_pdf": 1,
            "inspection_report": 1,
            "extracted_pages": 1,
            "ocr_result": 1,
            "normalized_document": 1,
        }
        assert {(row["stage"], row["state"], row["count"]) for row in mixed_jobs} == {
            ("preflight", "succeeded", 1),
            ("ocr", "succeeded", 1),
            ("chunk", "pending", 1),
        }
        assert {
            (task, dispatched_tenant, dispatched_version)
            for task, dispatched_tenant, dispatched_version, _ in dispatcher.calls
        } == {
            (PipelineTask.CHUNK_PROCESS, tenant_id, version_id),
            (PipelineTask.OCR_PROCESS, tenant_id, mixed_version_id),
            (PipelineTask.CHUNK_PROCESS, tenant_id, mixed_version_id),
        }
        assert attempt_outcomes == [(1, "lease_expired"), (2, "succeeded")]
        assert dict(encrypted_version) == {
            "state": "rejected",
            "terminal_reason_code": "PDF_ENCRYPTED_UNSUPPORTED",
        }
        assert encrypted_artifacts == ["inspection_report", "original_pdf"]
    finally:
        _delete_tenant(engine, tenant_id)
        engine.dispose()
        get_settings.cache_clear()
