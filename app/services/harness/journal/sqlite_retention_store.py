"""Transactional SQLite store for artifact retention evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.services.harness.journal.errors import RetentionStoreConflict
from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.journal.sqlite_retention_rows import (
    load_retention_evidence,
)
from app.services.harness.journal.sqlite_retention_writes import (
    authorize_collection,
    insert_blob,
    insert_hold,
    insert_reference,
    insert_tombstone,
    release_hold,
    release_reference,
)
from app.services.harness.protocol import Sha256, WorkspaceId
from app.services.harness.protocol.retention import (
    ArtifactLegalHold,
    ArtifactReference,
    ArtifactTombstone,
    RetainedBlob,
    RetentionEvidence,
)


class SQLiteRetentionStore:
    def __init__(self, connection_owner: SQLiteConnectionOwner) -> None:
        self._connection_owner = connection_owner

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 32,
    ) -> SQLiteRetentionStore:
        owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await owner.initialize()
        return cls(owner)

    async def close(self) -> None:
        await self._connection_owner.close()

    async def record_blob(self, blob: RetainedBlob) -> RetainedBlob:
        if blob.tombstone is not None or blob.garbage_collected_at is not None:
            raise ValueError("new retained blob cannot include lifecycle evidence")
        await self._connection_owner.execute(
            lambda connection: insert_blob(connection, blob)
        )
        evidence = await self.load(blob.workspace_id)
        stored = self._find_blob(evidence, blob.content_sha256)
        if stored != blob:
            raise RetentionStoreConflict(
                "blob digest already stores different evidence"
            )
        return stored

    async def add_reference(
        self,
        reference: ArtifactReference,
    ) -> ArtifactReference:
        if reference.released_at is not None:
            raise ValueError("new artifact reference cannot be released")
        await self._connection_owner.execute(
            lambda connection: insert_reference(connection, reference)
        )
        evidence = await self.load(reference.workspace_id)
        stored = self._find_reference(evidence, reference.reference_sha256)
        if stored != reference:
            raise RetentionStoreConflict(
                "reference digest already stores different evidence"
            )
        return stored

    async def release_reference(
        self,
        workspace_id: WorkspaceId,
        reference_sha256: Sha256,
        *,
        released_at: datetime,
    ) -> ArtifactReference:
        self._require_utc(released_at)
        await self._connection_owner.execute(
            lambda connection: release_reference(
                connection,
                workspace_id,
                reference_sha256,
                released_at,
            )
        )
        evidence = await self.load(workspace_id)
        return self._find_reference(evidence, reference_sha256)

    async def add_tombstone(
        self,
        tombstone: ArtifactTombstone,
    ) -> ArtifactTombstone:
        await self._connection_owner.execute(
            lambda connection: insert_tombstone(connection, tombstone)
        )
        evidence = await self.load(tombstone.workspace_id)
        blob = self._find_blob(evidence, tombstone.content_sha256)
        if blob.tombstone is None:
            raise RetentionStoreConflict("artifact tombstone was not stored")
        if blob.tombstone != tombstone:
            raise RetentionStoreConflict(
                "artifact tombstone conflicts with durable evidence"
            )
        return blob.tombstone

    async def place_hold(self, hold: ArtifactLegalHold) -> ArtifactLegalHold:
        if hold.released_at is not None:
            raise ValueError("new artifact legal hold cannot be released")
        await self._connection_owner.execute(
            lambda connection: insert_hold(connection, hold)
        )
        evidence = await self.load(hold.workspace_id)
        stored = self._find_hold(evidence, hold.hold_sha256)
        if stored != hold:
            raise RetentionStoreConflict(
                "hold digest already stores different evidence"
            )
        return stored

    async def release_hold(
        self,
        workspace_id: WorkspaceId,
        hold_sha256: Sha256,
        *,
        released_at: datetime,
    ) -> ArtifactLegalHold:
        self._require_utc(released_at)
        await self._connection_owner.execute(
            lambda connection: release_hold(
                connection,
                workspace_id,
                hold_sha256,
                released_at,
            )
        )
        evidence = await self.load(workspace_id)
        return self._find_hold(evidence, hold_sha256)

    async def load(self, workspace_id: WorkspaceId) -> RetentionEvidence:
        return await self._connection_owner.execute(
            lambda connection: load_retention_evidence(connection, workspace_id)
        )

    async def authorize_collection(
        self,
        workspace_id: WorkspaceId,
        content_sha256: Sha256,
        *,
        collected_at: datetime,
    ) -> RetainedBlob:
        self._require_utc(collected_at)
        await self._connection_owner.execute(
            lambda connection: authorize_collection(
                connection,
                workspace_id,
                content_sha256,
                collected_at,
            )
        )
        evidence = await self.load(workspace_id)
        return self._find_blob(evidence, content_sha256)

    @staticmethod
    def _find_blob(evidence: RetentionEvidence, digest: str) -> RetainedBlob:
        for blob in evidence.blobs:
            if blob.content_sha256 == digest:
                return blob
        raise RetentionStoreConflict("retained blob was not stored")

    @staticmethod
    def _find_reference(
        evidence: RetentionEvidence,
        digest: str,
    ) -> ArtifactReference:
        for reference in evidence.references:
            if reference.reference_sha256 == digest:
                return reference
        raise RetentionStoreConflict("artifact reference was not stored")

    @staticmethod
    def _find_hold(
        evidence: RetentionEvidence,
        digest: str,
    ) -> ArtifactLegalHold:
        for hold in evidence.legal_holds:
            if hold.hold_sha256 == digest:
                return hold
        raise RetentionStoreConflict("artifact legal hold was not stored")

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("retention release timestamp must use UTC")
