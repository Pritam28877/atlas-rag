from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.services.catalog.document_registration import (
    get_idempotent_version,
    get_or_create_document,
    validate_idempotent_match,
)
from app.services.catalog.errors import (
    CatalogForbiddenError,
    CatalogNotFoundError,
    CatalogQuotaError,
)
from app.services.catalog.status_query import VERSION_STATUS_SQL

EDITOR_ROLES = ("owner", "editor")


class CatalogRepository:
    """Execute tenant-scoped catalog changes inside caller-owned transactions."""

    async def create_collection(
        self,
        session: AsyncSession,
        principal: Principal,
        values: Mapping[str, Any],
        upload_max_bytes: int,
    ) -> Mapping[str, Any]:
        tenant_exists = await session.scalar(
            text("SELECT EXISTS(SELECT 1 FROM tenants WHERE id = :tenant_id)"),
            {"tenant_id": principal.tenant_id},
        )
        if not tenant_exists:
            raise CatalogForbiddenError("tenant is not provisioned")
        collection_id = uuid4()
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO collections (
                        id, tenant_id, name, retention_days, upload_max_bytes,
                        document_quota, storage_quota_bytes
                    ) VALUES (
                        :id, :tenant_id, :name, :retention_days, :upload_max_bytes,
                        :document_quota, :storage_quota_bytes
                    )
                    RETURNING *
                    """
                ),
                {
                    "id": collection_id,
                    "tenant_id": principal.tenant_id,
                    "upload_max_bytes": upload_max_bytes,
                    **values,
                },
            )
        ).mappings().one()
        await session.execute(
            text(
                """
                INSERT INTO collection_memberships (
                    id, tenant_id, collection_id, principal_subject, role
                ) VALUES (:id, :tenant_id, :collection_id, :subject, 'owner')
                """
            ),
            {
                "id": uuid4(),
                "tenant_id": principal.tenant_id,
                "collection_id": collection_id,
                "subject": principal.subject,
            },
        )
        return dict(row)

    async def list_collections(
        self,
        session: AsyncSession,
        principal: Principal,
        limit: int,
        cursor: tuple[datetime, UUID] | None,
    ) -> Sequence[Mapping[str, Any]]:
        cursor_created_at, cursor_id = cursor or (
            datetime.min.replace(tzinfo=UTC),
            UUID(int=0),
        )
        rows = await session.execute(
            text(
                """
                SELECT collection.*
                FROM collections AS collection
                JOIN collection_memberships AS membership
                  ON membership.tenant_id = collection.tenant_id
                 AND membership.collection_id = collection.id
                WHERE collection.tenant_id = :tenant_id
                  AND membership.principal_subject = :subject
                  AND (collection.created_at, collection.id) >
                      (:cursor_created_at, :cursor_id)
                ORDER BY collection.created_at, collection.id
                LIMIT :limit
                """
            ),
            {
                "tenant_id": principal.tenant_id,
                "subject": principal.subject,
                "cursor_created_at": cursor_created_at,
                "cursor_id": cursor_id,
                "limit": limit + 1,
            },
        )
        return [dict(row) for row in rows.mappings().all()]

    async def get_collection(
        self,
        session: AsyncSession,
        principal: Principal,
        collection_id: UUID,
        *,
        require_editor: bool = False,
        lock: bool = False,
    ) -> Mapping[str, Any]:
        role_clause = (
            "AND membership.role IN ('owner', 'editor')" if require_editor else ""
        )
        lock_clause = "FOR UPDATE OF collection" if lock else ""
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT collection.*, membership.role
                    FROM collections AS collection
                    JOIN collection_memberships AS membership
                      ON membership.tenant_id = collection.tenant_id
                     AND membership.collection_id = collection.id
                    WHERE collection.tenant_id = :tenant_id
                      AND collection.id = :collection_id
                      AND membership.principal_subject = :subject
                      {role_clause}
                    {lock_clause}
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "collection_id": collection_id,
                    "subject": principal.subject,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            raise CatalogNotFoundError("collection not found")
        return dict(row)

    async def register_document(
        self,
        session: AsyncSession,
        principal: Principal,
        collection_id: UUID,
        idempotency_key: str,
        version_id: UUID,
        values: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        collection = await self.get_collection(
            session,
            principal,
            collection_id,
            require_editor=True,
            lock=True,
        )
        existing = await get_idempotent_version(
            session, principal, collection_id, idempotency_key
        )
        if existing is not None:
            validate_idempotent_match(existing, values)
            return existing
        if values["size_bytes"] > collection["upload_max_bytes"]:
            raise CatalogQuotaError("upload exceeds collection size limit")
        counts = (
            await session.execute(
                text(
                    """
                    SELECT count(DISTINCT document.id) AS document_count,
                           COALESCE(sum(version.size_bytes), 0) AS storage_bytes
                    FROM source_documents AS document
                    LEFT JOIN document_versions AS version
                      ON version.tenant_id = document.tenant_id
                     AND version.collection_id = document.collection_id
                     AND version.document_id = document.id
                     AND version.state <> 'deleted'
                    WHERE document.tenant_id = :tenant_id
                      AND document.collection_id = :collection_id
                    """
                ),
                {"tenant_id": principal.tenant_id, "collection_id": collection_id},
            )
        ).mappings().one()
        document_exists = await session.scalar(
            text(
                """
                SELECT EXISTS(
                    SELECT 1 FROM source_documents
                    WHERE tenant_id = :tenant_id AND collection_id = :collection_id
                      AND source_key = :source_key
                )
                """
            ),
            {
                "tenant_id": principal.tenant_id,
                "collection_id": collection_id,
                "source_key": values["source_key"],
            },
        )
        document_quota_reached = (
            counts["document_count"] >= collection["document_quota"]
        )
        if not document_exists and document_quota_reached:
            raise CatalogQuotaError("collection document quota exceeded")
        projected_storage = counts["storage_bytes"] + values["size_bytes"]
        if projected_storage > collection["storage_quota_bytes"]:
            raise CatalogQuotaError("collection storage quota exceeded")
        document_id = await get_or_create_document(
            session, principal, collection_id, values
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
                "collection_id": collection_id,
                "document_id": document_id,
            },
        )
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO document_versions (
                        id, tenant_id, collection_id, document_id, version_number,
                        idempotency_key, content_sha256, size_bytes, object_key,
                        pipeline_profile
                    ) VALUES (
                        :id, :tenant_id, :collection_id, :document_id, :version_number,
                        :idempotency_key, :content_sha256, :size_bytes, :object_key,
                        :pipeline_profile
                    )
                    RETURNING *
                    """
                ),
                {
                    "id": version_id,
                    "tenant_id": principal.tenant_id,
                    "collection_id": collection_id,
                    "document_id": document_id,
                    "version_number": version_number,
                    "idempotency_key": idempotency_key,
                    "object_key": values["object_key"],
                    **values,
                },
            )
        ).mappings().one()
        return dict(row)

    async def recover_idempotent_registration(
        self,
        session: AsyncSession,
        principal: Principal,
        collection_id: UUID,
        idempotency_key: str,
        values: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        await self.get_collection(
            session,
            principal,
            collection_id,
            require_editor=True,
        )
        existing = await get_idempotent_version(
            session, principal, collection_id, idempotency_key
        )
        if existing is None:
            return None
        validate_idempotent_match(existing, values)
        return existing

    async def get_version(
        self,
        session: AsyncSession,
        principal: Principal,
        version_id: UUID,
        *,
        require_editor: bool = False,
        lock: bool = False,
    ) -> Mapping[str, Any]:
        role_clause = (
            "AND membership.role IN ('owner', 'editor')" if require_editor else ""
        )
        lock_clause = "FOR UPDATE OF version" if lock else ""
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT version.*
                    FROM document_versions AS version
                    JOIN collection_memberships AS membership
                      ON membership.tenant_id = version.tenant_id
                     AND membership.collection_id = version.collection_id
                    WHERE version.tenant_id = :tenant_id AND version.id = :version_id
                      AND membership.principal_subject = :subject
                      {role_clause}
                    {lock_clause}
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "version_id": version_id,
                    "subject": principal.subject,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            raise CatalogNotFoundError("document version not found")
        return dict(row)

    async def status(
        self,
        session: AsyncSession,
        principal: Principal,
        version_id: UUID,
    ) -> Mapping[str, Any]:
        await self.get_version(session, principal, version_id)
        row = (
            await session.execute(
                text(VERSION_STATUS_SQL),
                {"tenant_id": principal.tenant_id, "version_id": version_id},
            )
        ).mappings().one()
        return dict(row)
