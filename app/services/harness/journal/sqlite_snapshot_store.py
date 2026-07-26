"""Transactional SQLite persistence for synced replay snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.sqlite_connection import SQLiteConnectionOwner
from app.services.harness.journal.sqlite_snapshot_rows import (
    load_snapshot_bundle,
)
from app.services.harness.protocol import Sha256, WorkspaceId
from app.services.harness.protocol.retention import (
    SealedJournalSegment,
    SyncedSnapshot,
    SyncedSnapshotBundle,
)


class SnapshotStoreConflict(RuntimeError):
    """Stored snapshot facts conflict with the requested immutable bundle."""


class SQLiteSnapshotStore:
    def __init__(self, connection_owner: SQLiteConnectionOwner) -> None:
        self._connection_owner = connection_owner

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        maximum_pending_operations: int = 16,
    ) -> SQLiteSnapshotStore:
        connection_owner = SQLiteConnectionOwner(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            maximum_pending_operations=maximum_pending_operations,
        )
        await connection_owner.initialize()
        return cls(connection_owner)

    async def close(self) -> None:
        await self._connection_owner.close()

    async def save(
        self,
        bundle: SyncedSnapshotBundle,
    ) -> SyncedSnapshotBundle:
        return await self._connection_owner.execute(
            lambda connection: self._save(connection, bundle)
        )

    async def load_latest(
        self,
        workspace_id: WorkspaceId,
    ) -> SyncedSnapshotBundle | None:
        return await self._connection_owner.execute(
            lambda connection: self._load_latest(connection, workspace_id)
        )

    async def load(
        self,
        workspace_id: WorkspaceId,
        snapshot_sha256: Sha256,
    ) -> SyncedSnapshotBundle | None:
        return await self._connection_owner.execute(
            lambda connection: load_snapshot_bundle(
                connection,
                workspace_id,
                snapshot_sha256,
            )
        )

    def _save(
        self,
        connection: sqlite3.Connection,
        bundle: SyncedSnapshotBundle,
    ) -> SyncedSnapshotBundle:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = load_snapshot_bundle(
                connection,
                bundle.snapshot.workspace_id,
                bundle.snapshot.snapshot_sha256,
            )
            if existing is not None:
                if existing != bundle:
                    raise SnapshotStoreConflict(
                        "snapshot digest already stores different facts"
                    )
                connection.commit()
                return existing
            self._verify_replay_baseline(connection, bundle)
            self._insert_snapshot(connection, bundle.snapshot)
            for segment in bundle.segments:
                self._insert_segment(connection, segment)
            stored = load_snapshot_bundle(
                connection,
                bundle.snapshot.workspace_id,
                bundle.snapshot.snapshot_sha256,
            )
            if stored != bundle:
                raise SnapshotStoreConflict(
                    "snapshot facts conflict with durable state"
                )
            connection.commit()
            return stored
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise SnapshotStoreConflict(
                "snapshot facts conflict with durable state"
            ) from error
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _verify_replay_baseline(
        connection: sqlite3.Connection,
        bundle: SyncedSnapshotBundle,
    ) -> None:
        snapshot = bundle.snapshot
        if snapshot.through_journal_sequence > 0:
            row = connection.execute(
                """
                SELECT current_sequence
                FROM harness_journal_positions
                WHERE workspace_id = ?
                """,
                (snapshot.workspace_id,),
            ).fetchone()
            if (
                row is None
                or row["current_sequence"] < snapshot.through_journal_sequence
            ):
                raise SnapshotStoreConflict(
                    "snapshot replay baseline is not durable"
                )
            baseline = connection.execute(
                """
                SELECT 1 FROM harness_events
                WHERE workspace_id = ? AND journal_sequence = ?
                """,
                (
                    snapshot.workspace_id,
                    snapshot.through_journal_sequence,
                ),
            ).fetchone()
            if baseline is None:
                raise SnapshotStoreConflict(
                    "snapshot replay baseline does not identify an event"
                )
        for segment in bundle.segments:
            evidence = connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                       MIN(journal_sequence) AS first_sequence,
                       MAX(journal_sequence) AS last_sequence
                FROM harness_events
                WHERE workspace_id = ?
                  AND journal_sequence BETWEEN ? AND ?
                """,
                (
                    segment.workspace_id,
                    segment.first_journal_sequence,
                    segment.last_journal_sequence,
                ),
            ).fetchone()
            if (
                evidence is None
                or evidence["event_count"] != segment.event_count
                or evidence["first_sequence"] != segment.first_journal_sequence
                or evidence["last_sequence"] != segment.last_journal_sequence
            ):
                raise SnapshotStoreConflict(
                    "sealed segment does not match durable journal events"
                )

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        snapshot: SyncedSnapshot,
    ) -> None:
        reachable_json = json.dumps(
            snapshot.reachable_content_sha256s,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO harness_synced_snapshots (
                workspace_id, snapshot_sha256, manifest_sha256,
                through_journal_sequence, reachable_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.workspace_id,
                snapshot.snapshot_sha256,
                snapshot.manifest_sha256,
                snapshot.through_journal_sequence,
                reachable_json,
                snapshot.synced_at.isoformat(),
            ),
        )

    @staticmethod
    def _insert_segment(
        connection: sqlite3.Connection,
        segment: SealedJournalSegment,
    ) -> None:
        connection.execute(
            """
            INSERT INTO harness_sealed_segments (
                workspace_id, segment_sha256, snapshot_sha256,
                first_journal_sequence, last_journal_sequence,
                event_count, sealed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment.workspace_id,
                segment.segment_sha256,
                segment.snapshot_sha256,
                segment.first_journal_sequence,
                segment.last_journal_sequence,
                segment.event_count,
                segment.sealed_at.isoformat(),
            ),
        )

    @staticmethod
    def _load_latest(
        connection: sqlite3.Connection,
        workspace_id: str,
    ) -> SyncedSnapshotBundle | None:
        row = connection.execute(
            """
            SELECT snapshot_sha256
            FROM harness_synced_snapshots
            WHERE workspace_id = ?
            ORDER BY through_journal_sequence DESC
            LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
        if row is None:
            return None
        snapshot_sha256 = row["snapshot_sha256"]
        if not isinstance(snapshot_sha256, str):
            raise JournalStorageError("stored snapshot identity is invalid")
        return load_snapshot_bundle(connection, workspace_id, snapshot_sha256)
