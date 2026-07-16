import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.auth import Principal
from app.core.config import Settings
from app.core.storage import (
    PresignedUpload,
    StorageIntegrityError,
    StoredObjectMetadata,
)
from app.services.catalog.errors import CatalogValidationError
from app.services.catalog.pagination import decode_cursor, encode_cursor
from app.services.catalog.service import CatalogService
from app.workers.celery_app import PipelineTask

TENANT_ID = UUID("a7006ca9-bac4-4702-acaf-1e7c0dd6e7b6")
COLLECTION_ID = UUID("8c3dd611-c28f-4665-b680-bf47b07ee2f8")
DOCUMENT_ID = UUID("35ba671d-425f-46eb-946c-bb4501f7b5ad")
VERSION_ID = UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9")
JOB_ID = UUID("14391b0c-5f4c-4b02-9222-5308406cc2ad")
CHECKSUM = "a" * 64


class FakeDatabase:
    @asynccontextmanager
    async def transaction(self):
        yield object()


class FakeStorage:
    def __init__(self) -> None:
        self.prefix = b"%PDF-"
        self.content_type = "application/pdf"
        self.integrity_error = False

    def original_reference(self, tenant_id, version_id, checksum):
        assert tenant_id == TENANT_ID
        return type(
            "Reference",
            (),
            {"key": f"safe/{version_id}/{checksum}.pdf", "checksum_sha256": checksum},
        )()

    def presign_upload(self, reference, content_type):
        return PresignedUpload(
            url="https://storage.example.test/signed",
            headers={"content-type": content_type},
            expires_in_seconds=900,
        )

    async def verify_object(self, reference, expected_content_length):
        if self.integrity_error:
            raise StorageIntegrityError("mismatch")
        return StoredObjectMetadata(
            key=reference.key,
            content_length=expected_content_length,
            checksum_sha256=reference.checksum_sha256,
            content_type=self.content_type,
            version_id="storage-version",
            metadata={},
        )

    async def read_prefix(self, reference, byte_count):
        assert byte_count == 5
        return self.prefix


class FakeRepository:
    def __init__(self) -> None:
        self.version = {
            "id": VERSION_ID,
            "tenant_id": TENANT_ID,
            "collection_id": COLLECTION_ID,
            "document_id": DOCUMENT_ID,
            "content_sha256": CHECKSUM,
            "size_bytes": 1024,
            "object_key": f"safe/{VERSION_ID}/{CHECKSUM}.pdf",
            "pipeline_profile": "pdf-v1",
            "state": "received",
            "state_revision": 0,
            "progress_completed": 0,
            "progress_total": 0,
            "terminal_reason_code": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        self.raise_registration_conflict = False
        self.recovery_calls = 0

    async def get_version(self, session, principal, version_id, **kwargs):
        assert principal.tenant_id == TENANT_ID
        assert version_id == VERSION_ID
        return self.version

    async def register_document(
        self,
        session,
        principal,
        collection_id,
        idempotency_key,
        version_id,
        values,
    ):
        if self.raise_registration_conflict:
            raise IntegrityError("insert", {}, RuntimeError("unique conflict"))
        registered = dict(self.version)
        registered["id"] = version_id
        registered["object_key"] = values["object_key"]
        return registered

    async def recover_idempotent_registration(
        self,
        session,
        principal,
        collection_id,
        idempotency_key,
        values,
    ):
        self.recovery_calls += 1
        return self.version


class FakeOperations:
    def __init__(self) -> None:
        self.valid: bool | None = None
        self.reason_code: str | None = None

    async def complete_upload(
        self,
        session,
        principal,
        version_id,
        *,
        valid,
        reason_code,
        content_type,
        job_max_attempts,
    ):
        self.valid = valid
        self.reason_code = reason_code
        state = "queued" if valid else "rejected"
        version = {
            "id": version_id,
            "state": state,
            "terminal_reason_code": reason_code,
        }
        return version, JOB_ID if valid else None


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[object, UUID, UUID, UUID]] = []

    async def dispatch(self, task, tenant_id, version_id, job_id):
        self.calls.append((task, tenant_id, version_id, job_id))


def create_service():
    storage = FakeStorage()
    repository = FakeRepository()
    dispatcher = RecordingDispatcher()
    service = CatalogService(
        FakeDatabase(),
        storage,
        dispatcher,
        Settings(_env_file=None),
        repository,
    )
    operations = FakeOperations()
    service._operations = operations
    return service, storage, operations, dispatcher


def principal() -> Principal:
    return Principal(subject="user-123", tenant_id=TENANT_ID)


def test_valid_pdf_is_queued_after_bounded_preflight() -> None:
    service, _, operations, dispatcher = create_service()

    result = asyncio.run(service.complete_upload(principal(), VERSION_ID))

    assert result["state"] == "queued"
    assert operations.valid is True
    assert dispatcher.calls == [
        (PipelineTask.NATIVE_PROCESS, TENANT_ID, VERSION_ID, JOB_ID)
    ]


def test_invalid_magic_is_rejected_without_dispatch() -> None:
    service, storage, operations, dispatcher = create_service()
    storage.prefix = b"NOT-P"

    result = asyncio.run(service.complete_upload(principal(), VERSION_ID))

    assert result["state"] == "rejected"
    assert operations.reason_code == "PDF_MAGIC_INVALID"
    assert dispatcher.calls == []


def test_invalid_mime_is_rejected_without_dispatch() -> None:
    service, storage, operations, dispatcher = create_service()
    storage.content_type = "text/plain"

    asyncio.run(service.complete_upload(principal(), VERSION_ID))

    assert operations.reason_code == "UPLOAD_MIME_UNSUPPORTED"
    assert dispatcher.calls == []


def test_wrong_checksum_or_ownership_is_rejected_without_dispatch() -> None:
    service, storage, operations, dispatcher = create_service()
    storage.integrity_error = True

    asyncio.run(service.complete_upload(principal(), VERSION_ID))

    assert operations.reason_code == "UPLOAD_INTEGRITY_MISMATCH"
    assert dispatcher.calls == []


def test_registration_returns_storage_target_not_object_key() -> None:
    service, _, _, _ = create_service()
    values = {
        "source_key": "nda.pdf",
        "display_name": "NDA.pdf",
        "content_sha256": CHECKSUM,
        "size_bytes": 1024,
        "pipeline_profile": "pdf-v1",
    }

    result = asyncio.run(
        service.register_document(
            principal(), COLLECTION_ID, "request-0001", values
        )
    )

    assert result["upload"]["url"].startswith("https://storage.example.test/")
    assert "object_key" not in result


def test_registration_recovers_matching_concurrent_idempotency_conflict() -> None:
    service, _, _, _ = create_service()
    service._repository.raise_registration_conflict = True
    values = {
        "source_key": "nda.pdf",
        "display_name": "NDA.pdf",
        "content_sha256": CHECKSUM,
        "size_bytes": 1024,
        "pipeline_profile": "pdf-v1",
    }

    result = asyncio.run(
        service.register_document(
            principal(), COLLECTION_ID, "request-0001", values
        )
    )

    assert result["document_version_id"] == VERSION_ID
    assert service._repository.recovery_calls == 1


def test_pagination_cursor_round_trip_and_validation() -> None:
    created_at = datetime.now(UTC)

    assert decode_cursor(encode_cursor(created_at, VERSION_ID)) == (
        created_at,
        VERSION_ID,
    )

    with pytest.raises(CatalogValidationError):
        decode_cursor("not-a-valid-cursor")
