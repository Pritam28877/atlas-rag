"""Shared transactional state transitions and job creation for catalog APIs."""

from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.services.catalog.enums import VersionState
from app.services.catalog.errors import CatalogConflictError
from app.services.catalog.state_machine import (
    CatalogTransitionError,
    VersionSnapshot,
    transition_version,
)


async def transition_catalog_version(
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


async def create_ingestion_job(
    session: AsyncSession,
    principal: Principal,
    version: Mapping[str, object],
    *,
    stage: str,
    max_attempts: int,
    lifecycle_request_id: UUID | None = None,
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
                stage, generation, max_attempts, lifecycle_request_id
            ) VALUES (
                :id, :tenant_id, :collection_id, :version_id,
                :stage, :generation, :max_attempts, :lifecycle_request_id
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
            "lifecycle_request_id": lifecycle_request_id,
        },
    )
    return job_id
