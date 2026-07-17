"""Creation of immutable document versions for profile reprocessing."""

from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.services.catalog.enums import VersionState
from app.services.catalog.errors import CatalogConflictError
from app.services.catalog.state_machine import (
    CatalogTransitionError,
    validate_reprocessing_source,
)


async def create_reprocessed_version(
    session: AsyncSession,
    principal: Principal,
    source: Mapping[str, object],
    pipeline_profile: str,
    request_id: UUID,
) -> Mapping[str, object]:
    """Create a new chunk-ready version linked to verified normalized input."""
    try:
        validate_reprocessing_source(VersionState(str(source["state"])))
    except CatalogTransitionError as error:
        raise CatalogConflictError(str(error)) from error
    if str(source["pipeline_profile"]) == pipeline_profile:
        raise CatalogConflictError("reprocess requires a distinct pipeline profile")

    await session.execute(
        text(
            """
            SELECT id FROM source_documents
            WHERE tenant_id = :tenant_id AND collection_id = :collection_id
              AND id = :document_id
            FOR UPDATE
            """
        ),
        {
            "tenant_id": principal.tenant_id,
            "collection_id": source["collection_id"],
            "document_id": source["document_id"],
        },
    )

    deletion_in_progress = await session.scalar(
        text(
            """
            WITH RECURSIVE lineage AS (
                SELECT id, reprocessed_from_version_id, 0 AS depth
                FROM document_versions
                WHERE tenant_id = :tenant_id AND collection_id = :collection_id
                  AND id = :source_version_id
                UNION ALL
                SELECT parent.id, parent.reprocessed_from_version_id,
                       child.depth + 1
                FROM document_versions parent
                JOIN lineage child
                  ON parent.id = child.reprocessed_from_version_id
                WHERE parent.tenant_id = :tenant_id
                  AND parent.collection_id = :collection_id
                  AND child.depth < 100
            )
            SELECT EXISTS(
                SELECT 1 FROM ingestion_jobs job
                JOIN lineage ON lineage.id = job.document_version_id
                WHERE job.tenant_id = :tenant_id
                  AND job.collection_id = :collection_id
                  AND job.stage = 'delete'
                  AND job.state IN (
                    'pending', 'leased', 'running', 'retry_scheduled'
                  )
            )
            """
        ),
        {
            "tenant_id": principal.tenant_id,
            "collection_id": source["collection_id"],
            "source_version_id": source["id"],
        },
    )
    if deletion_in_progress:
        raise CatalogConflictError("cannot reprocess while deletion is in progress")

    active_descendant = await session.scalar(
        text(
            """
            SELECT EXISTS(
                SELECT 1 FROM document_versions
                WHERE tenant_id = :tenant_id
                  AND collection_id = :collection_id
                  AND reprocessed_from_version_id = :source_version_id
                  AND state NOT IN ('failed', 'cancelled', 'deleted')
            )
            """
        ),
        {
            "tenant_id": principal.tenant_id,
            "collection_id": source["collection_id"],
            "source_version_id": source["id"],
        },
    )
    if active_descendant:
        raise CatalogConflictError("reprocess source already has an active replacement")
    normalized_exists = await session.scalar(
        text(
            """
            WITH RECURSIVE lineage AS (
                SELECT id, reprocessed_from_version_id, 0 AS depth
                FROM document_versions
                WHERE tenant_id = :tenant_id AND collection_id = :collection_id
                  AND id = :version_id
                UNION ALL
                SELECT parent.id, parent.reprocessed_from_version_id,
                       lineage.depth + 1
                FROM document_versions parent
                JOIN lineage ON parent.id = lineage.reprocessed_from_version_id
                WHERE parent.tenant_id = :tenant_id
                  AND parent.collection_id = :collection_id
                  AND lineage.depth < 100
            )
            SELECT EXISTS(
                SELECT 1 FROM lineage
                JOIN artifacts ON artifacts.tenant_id = :tenant_id
                  AND artifacts.collection_id = :collection_id
                  AND artifacts.document_version_id = lineage.id
                  AND artifacts.artifact_type = 'normalized_document'
            )
            """
        ),
        {
            "tenant_id": principal.tenant_id,
            "collection_id": source["collection_id"],
            "version_id": source["id"],
        },
    )
    if not normalized_exists:
        raise CatalogConflictError(
            "reprocess source has no verified normalized artifact"
        )
    version_number = await session.scalar(
        text(
            """
            SELECT COALESCE(max(version_number), 0) + 1
            FROM document_versions
            WHERE tenant_id = :tenant_id AND collection_id = :collection_id
              AND document_id = :document_id
            """
        ),
        {
            "tenant_id": principal.tenant_id,
            "collection_id": source["collection_id"],
            "document_id": source["document_id"],
        },
    )
    target_id = uuid4()
    row = (
        await session.execute(
            text(
                """
                INSERT INTO document_versions (
                    id, tenant_id, collection_id, document_id, version_number,
                    idempotency_key, content_sha256, size_bytes, page_count,
                    detected_mime, object_key, source_uri_policy,
                    pipeline_profile, state, state_revision,
                    reprocessed_from_version_id
                ) VALUES (
                    :id, :tenant_id, :collection_id, :document_id, :version_number,
                    :idempotency_key, :content_sha256, :size_bytes, :page_count,
                    :detected_mime, :object_key, :source_uri_policy,
                    :pipeline_profile, 'chunking', 1, :source_version_id
                ) RETURNING *
                """
            ),
            {
                "id": target_id,
                "tenant_id": principal.tenant_id,
                "collection_id": source["collection_id"],
                "document_id": source["document_id"],
                "version_number": version_number,
                "idempotency_key": f"reprocess:{request_id}",
                "content_sha256": source["content_sha256"],
                "size_bytes": source["size_bytes"],
                "page_count": source["page_count"],
                "detected_mime": source["detected_mime"],
                "object_key": source["object_key"],
                "source_uri_policy": source["source_uri_policy"],
                "pipeline_profile": pipeline_profile,
                "source_version_id": source["id"],
            },
        )
    ).mappings().one()
    await session.execute(
        text(
            """
            INSERT INTO version_transition_events (
                id, tenant_id, collection_id, document_version_id,
                idempotency_key, from_state, to_state, from_revision,
                to_revision, operation, actor_type, actor_id
            ) VALUES (
                :id, :tenant_id, :collection_id, :version_id,
                :event_key, :from_state, 'chunking', 0, 1,
                'reprocess', 'api', :actor_id
            )
            """
        ),
        {
            "id": uuid4(),
            "tenant_id": principal.tenant_id,
            "collection_id": source["collection_id"],
            "version_id": target_id,
            "event_key": f"lifecycle:{request_id}",
            "from_state": source["state"],
            "actor_id": principal.subject,
        },
    )
    return dict(row)
