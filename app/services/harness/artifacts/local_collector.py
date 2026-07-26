"""Crash-retryable local artifact garbage collection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.services.harness.artifacts.local import LocalBlobStore
from app.services.harness.protocol import Sha256, WorkspaceId
from app.services.harness.protocol.retention import GarbageCollectionPlan


class CollectionAuthorizer(Protocol):
    async def authorize_collection(
        self,
        workspace_id: WorkspaceId,
        content_sha256: Sha256,
        *,
        collected_at: datetime,
    ) -> object: ...


class LocalGarbageCollector:
    def __init__(
        self,
        workspace_id: str,
        blob_store: LocalBlobStore,
        retention_store: CollectionAuthorizer,
        *,
        after_authorization: Callable[[str], None] | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._blob_store = blob_store
        self._retention_store = retention_store
        self._after_authorization = after_authorization

    async def collect(self, plan: GarbageCollectionPlan) -> tuple[str, ...]:
        if plan.workspace_id != self._workspace_id:
            raise ValueError("garbage collection plan workspace does not match")
        collected: list[str] = []
        for content_sha256 in plan.candidate_content_sha256s:
            await self._retention_store.authorize_collection(
                plan.workspace_id,
                content_sha256,
                collected_at=plan.planned_at,
            )
            if self._after_authorization is not None:
                self._after_authorization(content_sha256)
            await self._blob_store.delete(content_sha256)
            collected.append(content_sha256)
        return tuple(collected)
