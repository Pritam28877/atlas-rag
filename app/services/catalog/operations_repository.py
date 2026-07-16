from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.services.catalog.enums import VersionState
from app.services.catalog.errors import CatalogConflictError
from app.services.catalog.repository import CatalogRepository
from app.services.catalog.state_machine import (
    CatalogTransitionError,
    VersionSnapshot,
    transition_version,
)


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
        version = await self._transition(
            session,
            principal,
            version,
            VersionState.VALIDATING,
            operation="advance",
            reason_code=None,
            event_key="complete-upload:validating",
        )
        if not valid:
            version = await self._transition(
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
        version = await self._transition(
            session,
            principal,
            version,
            VersionState.QUEUED,
            operation="advance",
            reason_code=None,
            event_key="complete-upload:queued",
        )
        job_id = await self._create_job(
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
    ) -> tuple[Mapping[str, object], Mapping[str, object], UUID | None]:
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
                      AND request.idempotency_key = :idempotency_key
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "idempotency_key": idempotency_key,
                },
            )
        ).mappings().one_or_none()
        if existing is not None:
            identity_mismatch = existing["document_version_id"] != version_id
            operation_mismatch = existing["request_type"] != operation
            if identity_mismatch or operation_mismatch:
                raise CatalogConflictError(
                    "idempotency key was used for another request"
                )
            job_id = None
            if existing["state"] == "pending" and operation != "cancel":
                stage = "delete" if operation == "delete" else "preflight"
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
                        "version_id": version_id,
                        "stage": stage,
                    },
                )
            return dict(existing), {"state": existing["version_state"]}, job_id
        version = await self._catalog.get_version(
            session, principal, version_id, require_editor=True, lock=True
        )
        request_id = uuid4()
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
                        request_type, idempotency_key, requested_by, state,
                        completed_at
                    ) VALUES (
                        :id, :tenant_id, :collection_id, :version_id,
                        :operation, :idempotency_key, :subject, :state,
                        CASE WHEN :state = 'succeeded' THEN now() ELSE NULL END
                    ) RETURNING *
                    """
                ),
                {
                    "id": request_id,
                    "tenant_id": principal.tenant_id,
                    "collection_id": version["collection_id"],
                    "version_id": version_id,
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
            version = await self._transition(
                session,
                principal,
                version,
                VersionState.CANCELLED,
                operation="cancel",
                reason_code=None,
                event_key=f"lifecycle:{request_id}",
            )
        elif operation in {"retry", "reprocess"}:
            version = await self._transition(
                session,
                principal,
                version,
                VersionState.QUEUED,
                operation=operation,
                reason_code=None,
                event_key=f"lifecycle:{request_id}",
            )
            job_id = await self._create_job(
                session,
                principal,
                version,
                stage="preflight",
                max_attempts=job_max_attempts,
            )
        elif operation == "delete" and request_state == "pending":
            job_id = await self._create_job(
                session,
                principal,
                version,
                stage="delete",
                max_attempts=job_max_attempts,
            )
        return request, version, job_id

    async def _transition(
        self,
        session: AsyncSession,
        principal: Principal,
        version: Mapping[str, object],
        target: VersionState,
        *,
        operation: str,
        reason_code: str | None,
        event_key: str,
    ) -> Mapping[str, object]:
        snapshot = VersionSnapshot(
            state=VersionState(str(version["state"])),
            revision=int(str(version["state_revision"])),
            progress_completed=int(str(version["progress_completed"])),
            progress_total=int(str(version["progress_total"])),
            terminal_reason_code=(
                str(version["terminal_reason_code"])
                if version["terminal_reason_code"] is not None
                else None
            ),
        )
        try:
            transitioned = transition_version(
                snapshot,
                target,
                expected_revision=snapshot.revision,
                terminal_reason_code=reason_code,
                operation=operation,
            )
        except CatalogTransitionError as error:
            raise CatalogConflictError(str(error)) from error
        updated = (
            await session.execute(
                text(
                    """
                    UPDATE document_versions
                    SET state = :state, state_revision = :next_revision,
                        terminal_reason_code = :reason_code, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :version_id
                      AND state_revision = :expected_revision
                    RETURNING *
                    """
                ),
                {
                    "state": transitioned.state.value,
                    "next_revision": transitioned.revision,
                    "reason_code": transitioned.terminal_reason_code,
                    "tenant_id": principal.tenant_id,
                    "version_id": version["id"],
                    "expected_revision": snapshot.revision,
                },
            )
        ).mappings().one_or_none()
        if updated is None:
            raise CatalogConflictError("document version changed concurrently")
        await session.execute(
            text(
                """
                INSERT INTO version_transition_events (
                    id, tenant_id, collection_id, document_version_id,
                    idempotency_key, from_state, to_state, from_revision,
                    to_revision, operation, actor_type, actor_id
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :event_key, :from_state, :to_state, :from_revision,
                    :to_revision, :operation, 'api', :actor_id
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": principal.tenant_id,
                "collection_id": version["collection_id"],
                "version_id": version["id"],
                "event_key": event_key,
                "from_state": snapshot.state.value,
                "to_state": transitioned.state.value,
                "from_revision": snapshot.revision,
                "to_revision": transitioned.revision,
                "operation": operation,
                "actor_id": principal.subject,
            },
        )
        return dict(updated)

    async def _create_job(
        self,
        session: AsyncSession,
        principal: Principal,
        version: Mapping[str, object],
        *,
        stage: str,
        max_attempts: int,
    ) -> UUID:
        generation = await session.scalar(
            text(
                """
                SELECT COALESCE(max(generation), -1) + 1 FROM ingestion_jobs
                WHERE tenant_id = :tenant_id AND document_version_id = :version_id
                  AND stage = :stage
                """
            ),
            {
                "tenant_id": principal.tenant_id,
                "version_id": version["id"],
                "stage": stage,
            },
        )
        job_id = uuid4()
        await session.execute(
            text(
                """
                INSERT INTO ingestion_jobs (
                    id, tenant_id, collection_id, document_version_id,
                    stage, generation, max_attempts
                ) VALUES (
                    :id, :tenant_id, :collection_id, :version_id,
                    :stage, :generation, :max_attempts
                )
                """
            ),
            {
                "id": job_id,
                "tenant_id": principal.tenant_id,
                "collection_id": version["collection_id"],
                "version_id": version["id"],
                "stage": stage,
                "generation": generation,
                "max_attempts": max_attempts,
            },
        )
        return job_id
