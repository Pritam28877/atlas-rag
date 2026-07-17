import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from app.core.config import Settings, WorkerSettings
from app.core.database import Database
from app.core.storage import ObjectReference, ObjectStorage, sha256_hex
from app.services.catalog.enums import VersionState
from app.services.catalog.service import JobDispatcher
from app.services.ingestion.errors import RetryableIngestionError
from app.services.ingestion.parser import NativePdfParser
from app.services.ingestion.repository import NativeJobRepository
from app.services.ingestion.repository_support import ClaimedNativeJob
from app.services.ingestion.service import NativeIngestionService
from app.workers.celery_app import IngestionJobPayload

TENANT_ID = UUID("a7006ca9-bac4-4702-acaf-1e7c0dd6e7b6")
VERSION_ID = UUID("1dd21a4f-c0dc-45b4-8e36-5b9c9a7012a9")
JOB_ID = UUID("e4f1e42d-cc5f-48d7-b476-46743e94b5dc")
COLLECTION_ID = UUID("8d228d4b-e120-4f50-8878-98b5f0dc5063")
ARTIFACT_ID = UUID("18298549-f1b9-4316-960b-4449a7ba3911")


class FakeDatabase:
    @asynccontextmanager
    async def transaction(self):
        yield object()


class RecordingRepository:
    def __init__(self, job: ClaimedNativeJob | None) -> None:
        self.job = job
        self.retries: list[tuple[str, int]] = []
        self.failures: list[tuple[VersionState, str]] = []

    async def claim(self, *args, **kwargs):
        return self.job

    async def schedule_retry(
        self,
        session: object,
        job: ClaimedNativeJob,
        worker_id: str,
        reason_code: str,
        delay_seconds: int,
    ) -> None:
        self.retries.append((reason_code, delay_seconds))

    async def fail_permanently(
        self,
        session: object,
        job: ClaimedNativeJob,
        worker_id: str,
        state: VersionState,
        reason_code: str,
        artifacts=(),
    ) -> None:
        self.failures.append((state, reason_code))


class FailingDownloadStorage:
    def original_reference(
        self,
        tenant_id: UUID,
        document_version_id: UUID,
        checksum: str,
    ) -> ObjectReference:
        return ObjectReference("original.pdf", checksum, {})

    async def download_to_path(
        self,
        reference: ObjectReference,
        destination: Path,
        maximum_bytes: int,
    ) -> int:
        raise OSError("temporary storage failure")


class ParserCrashStorage(FailingDownloadStorage):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def download_to_path(
        self,
        reference: ObjectReference,
        destination: Path,
        maximum_bytes: int,
    ) -> int:
        destination.write_bytes(self.payload)
        return len(self.payload)


class CrashingParser:
    name = "crashing-parser"
    parser_version = "1.0"

    def parse(self, source: Path, policy, page_sink):
        raise RuntimeError("parser subprocess crashed")


class NoopDispatcher:
    async def dispatch(self, task, tenant_id, version_id, job_id) -> None:
        return None


def make_job(attempt_number: int = 1, max_attempts: int = 3) -> ClaimedNativeJob:
    payload = b"%PDF-worker-test"
    return ClaimedNativeJob(
        id=JOB_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        document_version_id=VERSION_ID,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        object_key="original.pdf",
        content_sha256=sha256_hex(payload),
        size_bytes=len(payload),
        pipeline_profile="pdf-v1",
        original_artifact_id=ARTIFACT_ID,
    )


def make_service(
    tmp_path: Path,
    repository: RecordingRepository,
    storage: object,
    parser: object,
) -> NativeIngestionService:
    settings = Settings.model_validate(
        {
            "workers": WorkerSettings(
                job_temp_directory=str(tmp_path),
                retry_base_seconds=2,
                retry_max_seconds=10,
            )
        }
    )
    return NativeIngestionService(
        cast(Database, FakeDatabase()),
        cast(ObjectStorage, storage),
        settings,
        cast(JobDispatcher, NoopDispatcher()),
        parser=cast(NativePdfParser, parser),
        repository=cast(NativeJobRepository, repository),
    )


@pytest.mark.parametrize(
    ("storage", "parser", "reason_code"),
    [
        (
            FailingDownloadStorage(),
            CrashingParser(),
            "INGESTION_DEPENDENCY_UNAVAILABLE",
        ),
        (
            ParserCrashStorage(b"%PDF-worker-test"),
            CrashingParser(),
            "NATIVE_PARSER_TEMPORARY_FAILURE",
        ),
    ],
)
def test_temporary_storage_and_parser_crashes_schedule_bounded_retry(
    tmp_path: Path,
    storage: object,
    parser: object,
    reason_code: str,
) -> None:
    repository = RecordingRepository(make_job())
    service = make_service(tmp_path, repository, storage, parser)
    payload = IngestionJobPayload(
        tenant_id=TENANT_ID,
        document_version_id=VERSION_ID,
        job_id=JOB_ID,
    )

    with pytest.raises(RetryableIngestionError) as captured:
        asyncio.run(service.process(payload, "native-worker"))

    assert captured.value.reason_code == reason_code
    assert 2 <= captured.value.retry_after_seconds <= 10
    assert repository.retries == [(reason_code, captured.value.retry_after_seconds)]
    assert list(tmp_path.iterdir()) == []


def test_retry_exhaustion_becomes_deterministic_terminal_failure(
    tmp_path: Path,
) -> None:
    repository = RecordingRepository(make_job(attempt_number=3, max_attempts=3))
    service = make_service(
        tmp_path,
        repository,
        FailingDownloadStorage(),
        CrashingParser(),
    )
    payload = IngestionJobPayload(
        tenant_id=TENANT_ID,
        document_version_id=VERSION_ID,
        job_id=JOB_ID,
    )

    asyncio.run(service.process(payload, "native-worker"))

    assert repository.retries == []
    assert repository.failures == [
        (VersionState.FAILED, "PROCESSING_RETRY_EXHAUSTED")
    ]
