import contextlib
import logging
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from prometheus_client import Counter, Histogram

from app.core.config import Settings
from app.core.database import Database
from app.core.storage import ArtifactKind, ObjectStorage, StorageIntegrityError
from app.services.catalog.enums import VersionState
from app.services.catalog.service import JobDispatcher
from app.services.ingestion.artifacts import (
    file_sha256,
    iter_page_artifact,
    merge_ocr_pages,
    write_ocr_pages,
)
from app.services.ingestion.errors import (
    JobFenceLostError,
    RetryableIngestionError,
    TerminalIngestionError,
)
from app.services.ingestion.models import PageOrigin
from app.services.ingestion.ocr_provider import (
    OcrProvider,
    OcrProviderTemporaryError,
    TesseractOcrProvider,
)
from app.services.ingestion.ocr_repository import ClaimedOcrJob, OcrJobRepository
from app.services.ingestion.repository_support import (
    NextStageJob,
    PublishedArtifact,
    defer_job_dispatch,
)
from app.workers.celery_app import IngestionJobPayload, PipelineTask
from app.workers.runtime import job_temp_directory

OCR_DOCUMENT_DURATION_SECONDS = Histogram(
    "rag_ocr_document_duration_seconds",
    "OCR document processing duration by bounded outcome.",
    ["outcome"],
)
OCR_DOCUMENTS_TOTAL = Counter(
    "rag_ocr_documents_total",
    "OCR documents processed by bounded outcome.",
    ["outcome"],
)
logger = logging.getLogger(__name__)


class OcrIngestionService:
    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        settings: Settings,
        dispatcher: JobDispatcher | None = None,
        provider: OcrProvider | None = None,
        repository: OcrJobRepository | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._settings = settings
        self._dispatcher = dispatcher
        self._provider = provider or TesseractOcrProvider()
        self._repository = repository or OcrJobRepository()

    async def process(self, payload: IngestionJobPayload, worker_id: str) -> None:
        lease_duration = timedelta(
            seconds=self._settings.workers.ocr_timeout_seconds + 30
        )
        async with self._database.transaction() as session:
            job = await self._repository.claim(
                session,
                payload.tenant_id,
                payload.document_version_id,
                payload.job_id,
                worker_id,
                lease_duration,
            )
            successor = None
            if job is None:
                successor = await self._repository.pending_successor(
                    session,
                    payload.tenant_id,
                    payload.document_version_id,
                    payload.job_id,
                )
        if job is None:
            if successor is not None and self._dispatcher is not None:
                await self._dispatch_or_defer(
                    payload.tenant_id,
                    payload.document_version_id,
                    successor,
                )
            return
        started = time.monotonic()
        try:
            next_job = await self._process_claimed_job(job, worker_id)
        except JobFenceLostError:
            return
        except TerminalIngestionError as error:
            self._record_document_metric("failed", started)
            async with self._database.transaction() as session:
                await self._repository.fail_permanently(
                    session,
                    job,
                    worker_id,
                    error.state,
                    error.reason_code,
                )
            return
        except StorageIntegrityError:
            self._record_document_metric("failed", started)
            async with self._database.transaction() as session:
                await self._repository.fail_permanently(
                    session,
                    job,
                    worker_id,
                    VersionState.FAILED,
                    "OCR_SOURCE_INTEGRITY_MISMATCH",
                )
            return
        except (
            BotoCoreError,
            ClientError,
            OSError,
            OcrProviderTemporaryError,
        ) as error:
            self._record_document_metric("retry", started)
            retry_scheduled = await self._retry_or_fail(
                job,
                worker_id,
                "OCR_PROVIDER_TEMPORARY_FAILURE",
            )
            if retry_scheduled:
                raise RetryableIngestionError(
                    "OCR_PROVIDER_TEMPORARY_FAILURE",
                    self._retry_delay(job),
                ) from error
            return
        except RetryableIngestionError:
            raise
        except Exception as error:
            self._record_document_metric("retry", started)
            retry_scheduled = await self._retry_or_fail(
                job,
                worker_id,
                "OCR_INTERNAL_FAILURE",
            )
            if retry_scheduled:
                raise RetryableIngestionError(
                    "OCR_INTERNAL_FAILURE",
                    self._retry_delay(job),
                ) from error
            return
        else:
            self._record_document_metric("success", started)
        if self._dispatcher is not None:
            await self._dispatch_or_defer(
                job.tenant_id,
                job.document_version_id,
                next_job,
            )

    async def _process_claimed_job(
        self,
        job: ClaimedOcrJob,
        worker_id: str,
    ) -> NextStageJob:
        temporary_root = Path(self._settings.workers.job_temp_directory)
        with job_temp_directory(temporary_root, job.id) as directory:
            source_pdf = directory / "document.pdf"
            extracted_path = directory / "extracted-pages.jsonl"
            original_reference = self._storage.original_reference(
                job.tenant_id,
                job.document_version_id,
                job.content_sha256,
            )
            extracted_reference = self._storage.artifact_reference(
                job.tenant_id,
                job.document_version_id,
                ArtifactKind.EXTRACTED_PAGES,
                job.extracted_generator_version,
                job.extracted_checksum_sha256,
            )
            downloaded_bytes = await self._storage.download_to_path(
                original_reference,
                source_pdf,
                min(job.size_bytes, self._settings.ingestion.upload_max_bytes),
            )
            if downloaded_bytes != job.size_bytes:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "OCR_SOURCE_INTEGRITY_MISMATCH",
                )
            await self._storage.download_to_path(
                extracted_reference,
                extracted_path,
                self._settings.ingestion.normalized_max_bytes,
            )
            selected_pages = tuple(
                page.page_number
                for page in iter_page_artifact(
                    extracted_path,
                    job.document_version_id,
                )
                if page.origin is PageOrigin.OCR
            )
            if not selected_pages:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "OCR_ELIGIBILITY_MISSING",
                )
            ocr_pages = self._provider.enrich_pages(
                source_pdf,
                selected_pages,
                self._settings.ingestion,
                directory,
            )
            if tuple(page.page_number for page in ocr_pages) != selected_pages:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "OCR_PAGE_COVERAGE_INCOMPLETE",
                )
            ocr_path = directory / "ocr-pages.jsonl"
            normalized_path = directory / "normalized-pages.jsonl"
            write_ocr_pages(
                ocr_path,
                job.document_version_id,
                self._provider.name,
                self._provider.provider_version,
                job.page_count,
                ocr_pages,
            )
            generator_version = self._generator_version()
            merge_ocr_pages(
                extracted_path,
                normalized_path,
                job.document_version_id,
                ocr_pages,
                generator_version,
            )
            artifacts = await self._publish_artifacts(
                job,
                generator_version,
                ocr_path,
                normalized_path,
            )
            try:
                async with self._database.transaction() as session:
                    next_job = await self._repository.succeed_ocr(
                        session,
                        job,
                        worker_id,
                        artifacts,
                        self._settings.workers.job_max_attempts,
                    )
            except JobFenceLostError:
                for artifact in artifacts:
                    with contextlib.suppress(Exception):
                        await self._storage.delete_key(artifact.object_key)
                raise
            return next_job

    async def _dispatch_or_defer(
        self,
        tenant_id: UUID,
        document_version_id: UUID,
        next_job: NextStageJob,
    ) -> None:
        assert self._dispatcher is not None
        try:
            await self._dispatcher.dispatch(
                PipelineTask.CHUNK_PROCESS,
                tenant_id,
                document_version_id,
                next_job.id,
            )
        except Exception:
            logger.warning(
                "deferred committed OCR successor after dispatch failure",
                extra={"job_id": str(next_job.id), "stage": next_job.stage},
            )
            async with self._database.transaction() as session:
                await defer_job_dispatch(
                    session,
                    tenant_id,
                    document_version_id,
                    next_job,
                    "CHUNK_DISPATCH_UNAVAILABLE",
                    timedelta(seconds=self._settings.workers.retry_base_seconds),
                )

    async def _publish_artifacts(
        self,
        job: ClaimedOcrJob,
        generator_version: str,
        ocr_path: Path,
        normalized_path: Path,
    ) -> list[PublishedArtifact]:
        pending = (
            ("ocr_result", ArtifactKind.OCR_RESULT, ocr_path),
            (
                "normalized_document",
                ArtifactKind.NORMALIZED_DOCUMENT,
                normalized_path,
            ),
        )
        artifacts: list[PublishedArtifact] = []
        for artifact_type, artifact_kind, path in pending:
            checksum = file_sha256(path)
            reference = self._storage.artifact_reference(
                job.tenant_id,
                job.document_version_id,
                artifact_kind,
                generator_version,
                checksum,
            )
            metadata = await self._storage.upload_from_path(
                reference,
                path,
                "application/json",
                self._settings.ingestion.normalized_max_bytes,
            )
            artifacts.append(
                PublishedArtifact(
                    artifact_type=artifact_type,
                    object_key=reference.key,
                    generator_name=self._provider.name,
                    generator_version=generator_version,
                    checksum_sha256=checksum,
                    size_bytes=metadata.content_length,
                )
            )
        return artifacts

    async def _retry_or_fail(
        self,
        job: ClaimedOcrJob,
        worker_id: str,
        reason_code: str,
    ) -> bool:
        async with self._database.transaction() as session:
            if job.attempt_number >= job.max_attempts:
                await self._repository.fail_permanently(
                    session,
                    job,
                    worker_id,
                    VersionState.FAILED,
                    "PROCESSING_RETRY_EXHAUSTED",
                )
                return False
            await self._repository.schedule_retry(
                session,
                job,
                worker_id,
                reason_code,
                self._retry_delay(job),
            )
        return True

    def _retry_delay(self, job: ClaimedOcrJob) -> int:
        exponential_delay = self._settings.workers.retry_base_seconds * (
            2 ** (job.attempt_number - 1)
        )
        return min(exponential_delay, self._settings.workers.retry_max_seconds)

    def _generator_version(self) -> str:
        version = self._provider.provider_version.replace(" ", "-")
        return f"pypdf+tesseract-{version}"

    @staticmethod
    def _record_document_metric(outcome: str, started: float) -> None:
        OCR_DOCUMENTS_TOTAL.labels(outcome).inc()
        OCR_DOCUMENT_DURATION_SECONDS.labels(outcome).observe(
            time.monotonic() - started
        )
