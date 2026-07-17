from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.services.catalog.enums import VersionState
from app.services.catalog.errors import CatalogConflictError
from app.services.catalog.operation_transitions import (
    create_ingestion_job,
    transition_catalog_version,
)
from app.services.catalog.repository import CatalogRepository
from app.services.catalog.reprocessing_repository import create_reprocessed_version


class CatalogOperationsRepository:
    def __init__(self, catalog: CatalogRepository) -> None:
        self._catalog = catalog

    async def complete_upload(
        self,
        session: AsyncSession,
        principal: Principal,
        version_id: UUID,
        *,
        valid: bool,
        reason_code: str | None,
        content_type: str,
        job_max_attempts: int,
    ) -> tuple[Mapping[str, object], UUID | None]:
        version = await self._catalog.get_version(
            session, principal, version_id, require_editor=True, lock=True
        )
        if version["state"] != VersionState.RECEIVED:
            job_id = await session.scalar(
                text(
                    """
                    SELECT id FROM ingestion_jobs
                    WHERE tenant_id = :tenant_id AND document_version_id = :version_id
                      AND stage = 'preflight'
                    ORDER BY generation DESC LIMIT 1
                    """
                ),
                {"tenant_id": principal.tenant_id, "version_id": version_id},
            )
            return version, job_id
        version = await transition_catalog_version(
            session,
            principal,
            version,
            VersionState.VALIDATING,
            operation="advance",
            reason_code=None,
            event_key="complete-upload:validating",
        )
        if not valid:
            version = await transition_catalog_version(
                session,
                principal,
                version,
                VersionState.REJECTED,
                operation="advance",
                reason_code=reason_code,
                event_key="complete-upload:rejected",
            )
            return version, None
        await session.execute(
            text(
                """
                INSERT INTO artifacts (
                    id, tenant_id, collection_id, document_version_id,
                    artifact_type, object_key, generator_name, generator_version,
                    checksum_sha256, size_bytes
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    'original_pdf', :object_key, 'intake-api', 'v1',
                    :checksum, :size_bytes
                ) ON CONFLICT ON CONSTRAINT uq_artifacts_output DO NOTHING
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": principal.tenant_id,
                "collection_id": version["collection_id"],
                "version_id": version_id,
                "object_key": version["object_key"],
                "checksum": version["content_sha256"],
                "size_bytes": version["size_bytes"],
            },
        )
        await session.execute(
            text(
                """
                UPDATE document_versions SET detected_mime = :content_type
                WHERE tenant_id = :tenant_id AND id = :version_id
                """
            ),
            {
                "content_type": content_type,
                "tenant_id": principal.tenant_id,
                "version_id": version_id,
            },
        )
        version = await transition_catalog_version(
            session,
            principal,
            version,
            VersionState.QUEUED,
            operation="advance",
            reason_code=None,
            event_key="complete-upload:queued",
        )
        job_id = await create_ingestion_job(
            session,
            principal,
            version,
            stage="preflight",
            max_attempts=job_max_attempts,
        )
        return version, job_id

    async def lifecycle_request(
        self,
        session: AsyncSession,
        principal: Principal,
        version_id: UUID,
        operation: str,
        idempotency_key: str,
        job_max_attempts: int,
        pipeline_profile: str | None = None,
    ) -> tuple[Mapping[str, object], Mapping[str, object], UUID | None]:
        source_version = await self._catalog.get_version(
            session, principal, version_id, require_editor=True
        )
        await session.execute(
            text(
                """
                SELECT id FROM source_documents
                WHERE tenant_id = :tenant_id
                  AND collection_id = :collection_id
                  AND id = :document_id
                FOR UPDATE
                """
            ),
            {
                "tenant_id": principal.tenant_id,
                "collection_id": source_version["collection_id"],
                "document_id": source_version["document_id"],
            },
        )
        source_version = await self._catalog.get_version(
            session, principal, version_id, require_editor=True, lock=True
        )
        existing = (
            await session.execute(
                text(
                    """
                    SELECT request.*, version.state AS version_state
                    FROM lifecycle_requests request
                    JOIN document_versions version
                      ON version.tenant_id = request.tenant_id
                     AND version.collection_id = request.collection_id
                     AND version.id = request.document_version_id
                    WHERE request.tenant_id = :tenant_id
                      AND request.collection_id = :collection_id
                      AND request.idempotency_key = :idempotency_key
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "collection_id": source_version["collection_id"],
                    "idempotency_key": idempotency_key,
                },
            )
        ).mappings().one_or_none()
        if existing is not None:
            identity_mismatch = existing["source_document_version_id"] != version_id
            operation_mismatch = existing["request_type"] != operation
            if identity_mismatch or operation_mismatch:
                raise CatalogConflictError(
                    "idempotency key was used for another request"
                )
            job_id = None
            if existing["state"] == "pending" and operation != "cancel":
                stage = {
                    "delete": "delete",
                    "reprocess": "chunk",
                }.get(operation, "preflight")
                job_id = await session.scalar(
                    text(
                        """
                        SELECT id FROM ingestion_jobs
                        WHERE tenant_id = :tenant_id
                          AND document_version_id = :version_id
                          AND stage = :stage
                        ORDER BY generation DESC LIMIT 1
                        """
                    ),
                    {
                        "tenant_id": principal.tenant_id,
                        "version_id": existing["document_version_id"],
                        "stage": stage,
                    },
                )
            return dict(existing), {"state": existing["version_state"]}, job_id
        if operation == "delete" and await self._has_active_deletion_job(
            session,
            principal.tenant_id,
            version_id,
        ):
            raise CatalogConflictError("deletion is already in progress")
        version = source_version
        request_id = uuid4()
        if operation == "reprocess":
            if pipeline_profile is None:
                raise CatalogConflictError(
                    "pipeline profile is required for reprocess"
                )
            version = await create_reprocessed_version(
                session,
                principal,
                source_version,
                pipeline_profile,
                request_id,
            )
        already_deleted = operation == "delete" and version["state"] == "deleted"
        request_state = (
            "succeeded" if operation == "cancel" or already_deleted else "pending"
        )
        request_row = (
            await session.execute(
                text(
                    """
                    INSERT INTO lifecycle_requests (
                        id, tenant_id, collection_id, document_version_id,
                        source_document_version_id, request_type,
                        idempotency_key, requested_by, state,
                        completed_at
                    ) VALUES (
                        :id, :tenant_id, :collection_id, :version_id,
                        :source_version_id, :operation, :idempotency_key,
                        :subject, :state,
                        CASE WHEN CAST(:state AS varchar) = 'succeeded'
                             THEN now() ELSE NULL END
                    ) RETURNING *
                    """
                ),
                {
                    "id": request_id,
                    "tenant_id": principal.tenant_id,
                    "collection_id": version["collection_id"],
                    "version_id": version["id"],
                    "source_version_id": source_version["id"],
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                    "subject": principal.subject,
                    "state": request_state,
                },
            )
        ).mappings().one()
        request = dict(request_row)
        job_id: UUID | None = None
        if operation == "cancel":
            version = await transition_catalog_version(
                session,
                principal,
                version,
                VersionState.CANCELLED,
                operation="cancel",
                reason_code=None,
                event_key=f"lifecycle:{request_id}",
            )
            await self._cancel_active_jobs(
                session, principal.tenant_id, UUID(str(version["id"]))
            )
        elif operation == "retry":
            version = await transition_catalog_version(
                session,
                principal,
                version,
                VersionState.QUEUED,
                operation=operation,
                reason_code=None,
                event_key=f"lifecycle:{request_id}",
            )
            job_id = await create_ingestion_job(
                session,
                principal,
                version,
                stage="preflight",
                max_attempts=job_max_attempts,
                lifecycle_request_id=request_id,
            )
        elif operation == "reprocess":
            job_id = await create_ingestion_job(
                session,
                principal,
                version,
                stage="chunk",
                max_attempts=job_max_attempts,
                lifecycle_request_id=request_id,
            )
        elif operation == "delete" and request_state == "pending":
            job_id = await create_ingestion_job(
                session,
                principal,
                version,
                stage="delete",
                max_attempts=job_max_attempts,
                lifecycle_request_id=request_id,
            )
        return request, version, job_id

    async def _has_active_deletion_job(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        version_id: UUID,
    ) -> bool:
        active_deletion = await session.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM ingestion_jobs
                    WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                      AND stage = 'delete'
                      AND state IN (
                          'pending', 'leased', 'running', 'retry_scheduled'
                      )
                )
                """
            ),
            {"tenant_id": tenant_id, "version_id": version_id},
        )
        return bool(active_deletion)

    async def _cancel_active_jobs(
        self, session: AsyncSession, tenant_id: UUID, version_id: UUID
    ) -> None:
        await session.execute(
            text(
                """
                WITH cancelled AS (
                    UPDATE ingestion_jobs
                    SET state = 'cancelled', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = now()
                    WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                      AND state IN ('pending', 'leased', 'running', 'retry_scheduled')
                    RETURNING id, attempt_count
                )
                UPDATE job_attempts attempt
                SET finished_at = now(), heartbeat_at = now(),
                    outcome = 'cancelled', reason_code = 'LIFECYCLE_CANCELLED'
                FROM cancelled
                WHERE attempt.tenant_id = :tenant_id
                  AND attempt.job_id = cancelled.id
                  AND attempt.attempt_number = cancelled.attempt_count
                  AND attempt.finished_at IS NULL
                """
            ),
            {"tenant_id": tenant_id, "version_id": version_id},
        )
