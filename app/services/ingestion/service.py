import contextlib
import logging
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.core.database import Database
from app.core.storage import ArtifactKind, ObjectStorage, StorageIntegrityError
from app.services.catalog.enums import VersionState
from app.services.catalog.service import JobDispatcher
from app.services.ingestion.artifacts import (
    PageArtifactWriter,
    file_sha256,
    write_inspection,
    write_terminal_inspection,
)
from app.services.ingestion.errors import (
    JobFenceLostError,
    RetryableIngestionError,
    TerminalIngestionError,
)
from app.services.ingestion.models import DocumentRoute, TerminalInspectionReport
from app.services.ingestion.parser import NativePdfParser, PypdfNativeParser
from app.services.ingestion.repository import NativeJobRepository
from app.services.ingestion.repository_support import (
    ClaimedNativeJob,
    NextStageJob,
    PublishedArtifact,
    defer_job_dispatch,
)
from app.workers.celery_app import IngestionJobPayload, PipelineTask
from app.workers.runtime import job_temp_directory

logger = logging.getLogger(__name__)


class NativeIngestionService:
    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        settings: Settings,
        dispatcher: JobDispatcher,
        parser: NativePdfParser | None = None,
        repository: NativeJobRepository | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._settings = settings
        self._dispatcher = dispatcher
        self._parser = parser or PypdfNativeParser()
        self._repository = repository or NativeJobRepository()

    async def process(self, payload: IngestionJobPayload, worker_id: str) -> None:
        lease_seconds = self._settings.workers.native_parse_timeout_seconds + 30
        async with self._database.transaction() as session:
            job = await self._repository.claim(
                session,
                payload.tenant_id,
                payload.document_version_id,
                payload.job_id,
                worker_id,
                lease_duration=self._seconds(lease_seconds),
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
            if successor is not None:
                await self._dispatch_or_defer(
                    payload.tenant_id,
                    payload.document_version_id,
                    successor,
                )
            return
        try:
            next_job = await self._process_claimed_job(job, worker_id)
        except JobFenceLostError:
            return
        except TerminalIngestionError as error:
            await self._record_terminal(job, worker_id, error)
            return
        except StorageIntegrityError:
            await self._record_terminal(
                job,
                worker_id,
                TerminalIngestionError(
                    VersionState.FAILED,
                    "UPLOAD_INTEGRITY_MISMATCH",
                ),
            )
            return
        except (BotoCoreError, ClientError, OSError) as error:
            retry_scheduled = await self._retry_or_fail(
                job, worker_id, "INGESTION_DEPENDENCY_UNAVAILABLE"
            )
            if retry_scheduled:
                raise self._retry_error(
                    job, "INGESTION_DEPENDENCY_UNAVAILABLE"
                ) from error
            return
        except RetryableIngestionError:
            raise
        except Exception as error:
            retry_scheduled = await self._retry_or_fail(
                job, worker_id, "NATIVE_PARSER_TEMPORARY_FAILURE"
            )
            if retry_scheduled:
                raise self._retry_error(
                    job, "NATIVE_PARSER_TEMPORARY_FAILURE"
                ) from error
            return
        await self._dispatch_or_defer(
            job.tenant_id,
            job.document_version_id,
            next_job,
        )

    async def _dispatch_or_defer(
        self,
        tenant_id: UUID,
        document_version_id: UUID,
        next_job: NextStageJob,
    ) -> None:
        task, reason_code = {
            "ocr": (PipelineTask.OCR_PROCESS, "OCR_DISPATCH_UNAVAILABLE"),
            "chunk": (PipelineTask.CHUNK_PROCESS, "CHUNK_DISPATCH_UNAVAILABLE"),
        }[next_job.stage]
        try:
            await self._dispatcher.dispatch(
                task,
                tenant_id,
                document_version_id,
                next_job.id,
            )
        except Exception:
            logger.warning(
                "deferred committed ingestion successor after dispatch failure",
                extra={"job_id": str(next_job.id), "stage": next_job.stage},
            )
            retry_delay = self._seconds(self._settings.workers.retry_base_seconds)
            async with self._database.transaction() as session:
                await defer_job_dispatch(
                    session,
                    tenant_id,
                    document_version_id,
                    next_job,
                    reason_code,
                    retry_delay,
                )

    async def _process_claimed_job(
        self, job: ClaimedNativeJob, worker_id: str
    ) -> NextStageJob:
        temporary_root = Path(self._settings.workers.job_temp_directory)
        with job_temp_directory(temporary_root, job.id) as directory:
            source_path = directory / "document.pdf"
            original_reference = self._storage.original_reference(
                job.tenant_id,
                job.document_version_id,
                job.content_sha256,
            )
            downloaded_bytes = await self._storage.download_to_path(
                original_reference,
                source_path,
                min(job.size_bytes, self._settings.ingestion.upload_max_bytes),
            )
            if downloaded_bytes != job.size_bytes:
                raise TerminalIngestionError(
                    VersionState.FAILED,
                    "UPLOAD_INTEGRITY_MISMATCH",
                )
            page_path = directory / "pages.jsonl"
            writer = PageArtifactWriter(
                page_path,
                job.document_version_id,
                self._parser.name,
                self._parser.parser_version,
            )
            try:
                inspection = self._parser.parse(
                    source_path,
                    self._settings.ingestion,
                    writer.write_page,
                )
                writer.finish(inspection)
            finally:
                writer.close()
            inspection_path = directory / "inspection.json"
            write_inspection(inspection_path, inspection)
            artifacts = await self._publish_artifacts(
                job,
                inspection_path,
                page_path,
                inspection.route,
            )
            try:
                async with self._database.transaction() as session:
                    return await self._repository.succeed(
                        session,
                        job,
                        worker_id=worker_id,
                        route=inspection.route,
                        page_count=inspection.page_count,
                        artifacts=artifacts,
                        max_attempts=self._settings.workers.job_max_attempts,
                    )
            except JobFenceLostError:
                await self._remove_uncommitted_artifacts(artifacts)
                raise

    async def _publish_artifacts(
        self,
        job: ClaimedNativeJob,
        inspection_path: Path,
        page_path: Path,
        route: DocumentRoute,
    ) -> list[PublishedArtifact]:
        generator_version = f"pypdf-{self._parser.parser_version}"
        pending = [
            ("inspection_report", ArtifactKind.INSPECTION, inspection_path),
            ("extracted_pages", ArtifactKind.EXTRACTED_PAGES, page_path),
        ]
        if route is DocumentRoute.NATIVE:
            pending.append(
                (
                    "normalized_document",
                    ArtifactKind.NORMALIZED_DOCUMENT,
                    page_path,
                )
            )
        published: list[PublishedArtifact] = []
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
            published.append(
                PublishedArtifact(
                    artifact_type=artifact_type,
                    object_key=reference.key,
                    generator_name=self._parser.name,
                    generator_version=generator_version,
                    checksum_sha256=checksum,
                    size_bytes=metadata.content_length,
                )
            )
        return published

    async def _remove_uncommitted_artifacts(
        self, artifacts: list[PublishedArtifact]
    ) -> None:
        for artifact in artifacts:
            with contextlib.suppress(Exception):
                await self._storage.delete_key(artifact.object_key)

    async def _publish_terminal_inspection(
        self,
        job: ClaimedNativeJob,
        error: TerminalIngestionError,
    ) -> PublishedArtifact:
        generator_version = f"pypdf-{self._parser.parser_version}"
        temporary_root = Path(self._settings.workers.job_temp_directory)
        with job_temp_directory(temporary_root, job.id) as directory:
            inspection_path = directory / "terminal-inspection.json"
            write_terminal_inspection(
                inspection_path,
                TerminalInspectionReport(
                    parser_name=self._parser.name,
                    parser_version=self._parser.parser_version,
                    outcome_state=error.state,
                    reason_code=error.reason_code,
                ),
            )
            checksum = file_sha256(inspection_path)
            reference = self._storage.artifact_reference(
                job.tenant_id,
                job.document_version_id,
                ArtifactKind.INSPECTION,
                generator_version,
                checksum,
            )
            metadata = await self._storage.upload_from_path(
                reference,
                inspection_path,
                "application/json",
                self._settings.ingestion.normalized_max_bytes,
            )
        return PublishedArtifact(
            artifact_type="inspection_report",
            object_key=reference.key,
            generator_name=self._parser.name,
            generator_version=generator_version,
            checksum_sha256=checksum,
            size_bytes=metadata.content_length,
        )

    async def _record_terminal(
        self,
        job: ClaimedNativeJob,
        worker_id: str,
        error: TerminalIngestionError,
    ) -> None:
        try:
            inspection_artifact = await self._publish_terminal_inspection(job, error)
        except (
            BotoCoreError,
            ClientError,
            OSError,
            StorageIntegrityError,
        ) as storage_error:
            retry_scheduled = await self._retry_or_fail(
                job,
                worker_id,
                "INGESTION_DEPENDENCY_UNAVAILABLE",
            )
            if retry_scheduled:
                raise self._retry_error(
                    job,
                    "INGESTION_DEPENDENCY_UNAVAILABLE",
                ) from storage_error
            return
        async with self._database.transaction() as session:
            await self._repository.fail_permanently(
                session,
                job,
                worker_id,
                error.state,
                error.reason_code,
                artifacts=[inspection_artifact],
            )

    async def _retry_or_fail(
        self,
        job: ClaimedNativeJob,
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

    def _retry_error(
        self, job: ClaimedNativeJob, reason_code: str
    ) -> RetryableIngestionError:
        return RetryableIngestionError(reason_code, self._retry_delay(job))

    def _retry_delay(self, job: ClaimedNativeJob) -> int:
        exponential_delay = self._settings.workers.retry_base_seconds * (
            2 ** (job.attempt_number - 1)
        )
        bounded_delay = min(
            exponential_delay,
            self._settings.workers.retry_max_seconds,
        )
        jitter_ceiling = max(bounded_delay // 4, 1)
        deterministic_jitter = job.id.int % jitter_ceiling
        return min(
            bounded_delay + deterministic_jitter,
            self._settings.workers.retry_max_seconds,
        )

    @staticmethod
    def _seconds(value: int) -> timedelta:
        return timedelta(seconds=value)
