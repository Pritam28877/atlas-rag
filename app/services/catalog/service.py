import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import IntegrityError

from app.core.auth import Principal
from app.core.config import Settings
from app.core.database import Database
from app.core.storage import ObjectStorage, StorageIntegrityError
from app.services.catalog.errors import (
    CatalogConflictError,
    CatalogDependencyError,
)
from app.services.catalog.operations_repository import CatalogOperationsRepository
from app.services.catalog.pagination import decode_cursor, encode_cursor
from app.services.catalog.repository import CatalogRepository
from app.workers.celery_app import (
    IngestionJobPayload,
    IngestionTaskDispatcher,
    PipelineTask,
)


class JobDispatcher(Protocol):
    async def dispatch(
        self,
        task: PipelineTask,
        tenant_id: UUID,
        version_id: UUID,
        job_id: UUID,
    ) -> None: ...


@dataclass(slots=True)
class CeleryJobDispatcher:
    dispatcher: IngestionTaskDispatcher

    async def dispatch(
        self,
        task: PipelineTask,
        tenant_id: UUID,
        version_id: UUID,
        job_id: UUID,
    ) -> None:
        payload = IngestionJobPayload(
            tenant_id=tenant_id,
            document_version_id=version_id,
            job_id=job_id,
        )
        await asyncio.to_thread(
            self.dispatcher.dispatch,
            task,
            payload,
        )


class CatalogService:
    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        dispatcher: JobDispatcher,
        settings: Settings,
        repository: CatalogRepository | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._dispatcher = dispatcher
        self._settings = settings
        self._repository = repository or CatalogRepository()
        self._operations = CatalogOperationsRepository(self._repository)

    async def create_collection(
        self, principal: Principal, values: Mapping[str, object]
    ) -> Mapping[str, object]:
        try:
            async with self._database.transaction() as session:
                return await self._repository.create_collection(
                    session,
                    principal,
                    values,
                    self._settings.ingestion.upload_max_bytes,
                )
        except IntegrityError as error:
            raise CatalogConflictError("collection name already exists") from error

    async def list_collections(
        self,
        principal: Principal,
        limit: int,
        cursor_value: str | None,
    ) -> tuple[list[Mapping[str, object]], str | None]:
        cursor = decode_cursor(cursor_value) if cursor_value else None
        async with self._database.transaction() as session:
            rows = list(
                await self._repository.list_collections(
                    session, principal, limit, cursor
                )
            )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(last["created_at"], last["id"])
        return page, next_cursor

    async def get_collection(
        self, principal: Principal, collection_id: UUID
    ) -> Mapping[str, object]:
        async with self._database.transaction() as session:
            return await self._repository.get_collection(
                session, principal, collection_id
            )

    async def register_document(
        self,
        principal: Principal,
        collection_id: UUID,
        idempotency_key: str,
        values: Mapping[str, object],
    ) -> Mapping[str, object]:
        version_id = uuid4()
        reference = self._storage.original_reference(
            principal.tenant_id,
            version_id,
            str(values["content_sha256"]),
        )
        repository_values = dict(values)
        repository_values["object_key"] = reference.key
        try:
            async with self._database.transaction() as session:
                version = await self._repository.register_document(
                    session,
                    principal,
                    collection_id,
                    idempotency_key,
                    version_id,
                    repository_values,
                )
        except IntegrityError as error:
            async with self._database.transaction() as session:
                version = await self._repository.recover_idempotent_registration(
                    session,
                    principal,
                    collection_id,
                    idempotency_key,
                    repository_values,
                )
            if version is None:
                raise CatalogConflictError(
                    "document registration conflicted"
                ) from error
        reference = self._storage.original_reference(
            principal.tenant_id,
            version["id"],
            version["content_sha256"],
        )
        upload = self._storage.presign_upload(reference, "application/pdf")
        return {
            "document_id": version["document_id"],
            "document_version_id": version["id"],
            "state": version["state"],
            "upload": {
                "url": upload.url,
                "headers": dict(upload.headers),
                "expires_in_seconds": upload.expires_in_seconds,
            },
        }

    async def complete_upload(
        self, principal: Principal, version_id: UUID
    ) -> Mapping[str, object]:
        async with self._database.transaction() as session:
            version = await self._repository.get_version(
                session, principal, version_id, require_editor=True
            )
        if version["state"] != "received":
            async with self._database.transaction() as session:
                completed, job_id = await self._operations.complete_upload(
                    session,
                    principal,
                    version_id,
                    valid=True,
                    reason_code=None,
                    content_type="application/pdf",
                    job_max_attempts=self._settings.workers.job_max_attempts,
                )
            if job_id is not None and completed["state"] == "queued":
                await self._dispatch(
                    PipelineTask.NATIVE_PROCESS,
                    principal.tenant_id,
                    version_id,
                    job_id,
                )
            return self._completion_response(completed, job_id)
        reference = self._storage.original_reference(
            principal.tenant_id,
            version_id,
            version["content_sha256"],
        )
        valid = True
        reason_code: str | None = None
        content_type = "application/pdf"
        try:
            metadata = await self._storage.verify_object(
                reference, expected_content_length=version["size_bytes"]
            )
            content_type = metadata.content_type or ""
            prefix = await self._storage.read_prefix(reference, 5)
            if content_type not in self._settings.ingestion.allowed_mime_types:
                valid = False
                reason_code = "UPLOAD_MIME_UNSUPPORTED"
            elif prefix != b"%PDF-":
                valid = False
                reason_code = "PDF_MAGIC_INVALID"
        except StorageIntegrityError:
            valid = False
            reason_code = "UPLOAD_INTEGRITY_MISMATCH"
        except (BotoCoreError, ClientError) as error:
            raise CatalogDependencyError(
                "object storage verification failed"
            ) from error
        async with self._database.transaction() as session:
            completed, job_id = await self._operations.complete_upload(
                session,
                principal,
                version_id,
                valid=valid,
                reason_code=reason_code,
                content_type=content_type,
                job_max_attempts=self._settings.workers.job_max_attempts,
            )
        if job_id is not None:
            await self._dispatch(
                PipelineTask.NATIVE_PROCESS,
                principal.tenant_id,
                version_id,
                job_id,
            )
        return self._completion_response(completed, job_id)

    async def status(
        self, principal: Principal, version_id: UUID
    ) -> Mapping[str, object]:
        async with self._database.transaction() as session:
            row = await self._repository.status(session, principal, version_id)
        return {
            "document_version_id": row["id"],
            "document_id": row["document_id"],
            "collection_id": row["collection_id"],
            "state": row["state"],
            "stage": row["stage"],
            "progress_completed": row["progress_completed"],
            "progress_total": row["progress_total"],
            "terminal_reason_code": row["terminal_reason_code"],
            "retry_eligible": row["state"] == "failed",
            "artifacts_available": row["artifacts_available"],
            "citations_ready": row["citations_ready"],
            "lexical_index_ready": row["lexical_index_ready"],
            "vector_index_ready": row["vector_index_ready"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def lifecycle(
        self,
        principal: Principal,
        version_id: UUID,
        operation: str,
        idempotency_key: str,
    ) -> Mapping[str, object]:
        async with self._database.transaction() as session:
            request, version, job_id = await self._operations.lifecycle_request(
                session,
                principal,
                version_id,
                operation,
                idempotency_key,
                self._settings.workers.job_max_attempts,
            )
        if job_id is not None:
            task = (
                PipelineTask.DELETE_DOCUMENT
                if operation == "delete"
                else PipelineTask.NATIVE_PROCESS
            )
            await self._dispatch(task, principal.tenant_id, version_id, job_id)
        return {
            "operation_id": request["id"],
            "document_version_id": version_id,
            "operation": operation,
            "status": request["state"],
            "version_state": version["state"],
        }

    def _completion_response(
        self, version: Mapping[str, object], job_id: UUID | None
    ) -> Mapping[str, object]:
        return {
            "document_version_id": version["id"],
            "job_id": job_id,
            "state": version["state"],
            "reason_code": version["terminal_reason_code"],
        }

    async def _dispatch(
        self,
        task: PipelineTask,
        tenant_id: UUID,
        version_id: UUID,
        job_id: UUID,
    ) -> None:
        try:
            await self._dispatcher.dispatch(task, tenant_id, version_id, job_id)
        except Exception as error:
            raise CatalogDependencyError(
                "job dispatch is temporarily unavailable"
            ) from error
