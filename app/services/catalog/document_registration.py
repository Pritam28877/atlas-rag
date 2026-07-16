from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.services.catalog.errors import CatalogConflictError


async def get_idempotent_version(
    session: AsyncSession,
    principal: Principal,
    collection_id: UUID,
    idempotency_key: str,
) -> Mapping[str, Any] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT version.*, document.source_key
                FROM document_versions version
                JOIN source_documents document
                  ON document.tenant_id = version.tenant_id
                 AND document.collection_id = version.collection_id
                 AND document.id = version.document_id
                WHERE version.tenant_id = :tenant_id
                  AND version.collection_id = :collection_id
                  AND version.idempotency_key = :idempotency_key
                """
            ),
            {
                "tenant_id": principal.tenant_id,
                "collection_id": collection_id,
                "idempotency_key": idempotency_key,
            },
        )
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def validate_idempotent_match(
    existing: Mapping[str, Any], values: Mapping[str, Any]
) -> None:
    compared_fields = (
        "source_key",
        "content_sha256",
        "size_bytes",
        "pipeline_profile",
    )
    if any(existing[field] != values[field] for field in compared_fields):
        raise CatalogConflictError("idempotency key was used for another request")


async def get_or_create_document(
    session: AsyncSession,
    principal: Principal,
    collection_id: UUID,
    values: Mapping[str, Any],
) -> UUID:
    existing = await session.scalar(
        text(
            """
            SELECT id FROM source_documents
            WHERE tenant_id = :tenant_id AND collection_id = :collection_id
              AND source_key = :source_key
            FOR UPDATE
            """
        ),
        {
            "tenant_id": principal.tenant_id,
            "collection_id": collection_id,
            "source_key": values["source_key"],
        },
    )
    if existing is not None:
        return existing
    document_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO source_documents (
                id, tenant_id, collection_id, source_key, display_name
            ) VALUES (:id, :tenant_id, :collection_id, :source_key, :display_name)
            """
        ),
        {
            "id": document_id,
            "tenant_id": principal.tenant_id,
            "collection_id": collection_id,
            "source_key": values["source_key"],
            "display_name": values["display_name"],
        },
    )
    return document_id
