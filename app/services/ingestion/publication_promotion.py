"""Atomic catalog-side promotion for a successfully reprocessed version."""

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ingestion.publication_models import ClaimedPublicationJob


async def supersede_source_version(
    session: AsyncSession,
    job: ClaimedPublicationJob,
    worker_id: str,
) -> None:
    source = (
        await session.execute(
            text(
                """
                WITH previous AS (
                    SELECT id, state, state_revision FROM document_versions
                    WHERE tenant_id = :tenant_id
                      AND collection_id = :collection_id
                      AND id = :source_version_id
                      AND state IN ('ready', 'ready_with_warnings')
                    FOR UPDATE
                )
                UPDATE document_versions version
                SET state = 'superseded',
                    state_revision = previous.state_revision + 1,
                    updated_at = now()
                FROM previous WHERE version.id = previous.id
                RETURNING previous.state AS previous_state,
                          previous.state_revision AS previous_revision
                """
            ),
            {
                "tenant_id": job.tenant_id,
                "collection_id": job.collection_id,
                "source_version_id": job.source_document_version_id,
            },
        )
    ).mappings().one_or_none()
    await session.execute(
        text(
            """
            UPDATE index_publications SET deleted_at = COALESCE(deleted_at, now())
            WHERE tenant_id = :tenant_id AND collection_id = :collection_id
              AND document_version_id = :source_version_id
            """
        ),
        {
            "tenant_id": job.tenant_id,
            "collection_id": job.collection_id,
            "source_version_id": job.source_document_version_id,
        },
    )
    if source is None:
        raise ValueError("reprocess source is no longer promotable")
    previous_revision = int(str(source["previous_revision"]))
    await session.execute(
        text(
            """
            INSERT INTO version_transition_events (
                id, tenant_id, collection_id, document_version_id,
                idempotency_key, from_state, to_state, from_revision,
                to_revision, operation, actor_type, actor_id
            ) VALUES (
                :id, :tenant_id, :collection_id, :source_version_id,
                :event_key, :from_state, 'superseded', :from_revision,
                :to_revision, 'supersede', 'worker', :worker_id
            )
            """
        ),
        {
            "id": uuid4(),
            "tenant_id": job.tenant_id,
            "collection_id": job.collection_id,
            "source_version_id": job.source_document_version_id,
            "event_key": f"worker:{job.id}:promote",
            "from_state": source["previous_state"],
            "from_revision": previous_revision,
            "to_revision": previous_revision + 1,
            "worker_id": worker_id,
        },
    )
