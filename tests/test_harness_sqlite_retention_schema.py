import asyncio
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import SQLiteEventJournal
from scripts.probe_harness_sqlite_kill import NOW

WORKSPACE_ID = "wsp_" + "0" * 32


def digest(number: int) -> str:
    return f"{number:064x}"


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def initialize(path: Path) -> None:
    async def scenario() -> None:
        journal = await SQLiteEventJournal.open(path)
        await journal.close()

    asyncio.run(scenario())


def insert_blob(connection: sqlite3.Connection, number: int = 1) -> None:
    connection.execute(
        """
        INSERT INTO harness_retained_blobs (
            workspace_id, content_sha256, size_bytes, created_at
        ) VALUES (?, ?, 1024, ?)
        """,
        (WORKSPACE_ID, digest(number), NOW.isoformat()),
    )


def test_retention_evidence_requires_workspace_scoped_blob(
    tmp_path: Path,
) -> None:
    path = database_path(tmp_path)
    initialize(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO harness_artifact_references (
                    workspace_id, reference_sha256, content_sha256, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (WORKSPACE_ID, digest(10), digest(1), NOW.isoformat()),
            )
    finally:
        connection.close()


def test_collection_requires_snapshot_tombstone_and_released_protection(
    tmp_path: Path,
) -> None:
    path = database_path(tmp_path)
    initialize(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    collected_at = NOW + timedelta(days=3)
    try:
        insert_blob(connection)
        connection.execute(
            """
            INSERT INTO harness_synced_snapshots (
                workspace_id, snapshot_sha256, manifest_sha256,
                through_journal_sequence, reachable_json, synced_at
            ) VALUES (?, ?, ?, 0, '[]', ?)
            """,
            (WORKSPACE_ID, digest(20), digest(21), NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO harness_artifact_tombstones (
                workspace_id, content_sha256, tombstoned_at,
                delete_after, reason
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                WORKSPACE_ID,
                digest(1),
                (NOW + timedelta(days=1)).isoformat(),
                (NOW + timedelta(days=2)).isoformat(),
                "Retention expired.",
            ),
        )
        connection.execute(
            """
            INSERT INTO harness_artifact_references (
                workspace_id, reference_sha256, content_sha256, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (WORKSPACE_ID, digest(30), digest(1), NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO harness_artifact_legal_holds (
                workspace_id, hold_sha256, content_sha256, placed_at
            ) VALUES (?, ?, ?, ?)
            """,
            (WORKSPACE_ID, digest(40), digest(1), NOW.isoformat()),
        )

        with pytest.raises(sqlite3.IntegrityError, match="not collectable"):
            connection.execute(
                """
                UPDATE harness_retained_blobs
                SET garbage_collected_at = ?
                WHERE workspace_id = ? AND content_sha256 = ?
                """,
                (collected_at.isoformat(), WORKSPACE_ID, digest(1)),
            )
        released_at = (NOW + timedelta(days=1)).isoformat()
        connection.execute(
            """
            UPDATE harness_artifact_references SET released_at = ?
            WHERE workspace_id = ? AND reference_sha256 = ?
            """,
            (released_at, WORKSPACE_ID, digest(30)),
        )
        connection.execute(
            """
            UPDATE harness_artifact_legal_holds SET released_at = ?
            WHERE workspace_id = ? AND hold_sha256 = ?
            """,
            (released_at, WORKSPACE_ID, digest(40)),
        )
        connection.execute(
            """
            UPDATE harness_retained_blobs SET garbage_collected_at = ?
            WHERE workspace_id = ? AND content_sha256 = ?
            """,
            (collected_at.isoformat(), WORKSPACE_ID, digest(1)),
        )
        stored = connection.execute(
            """
            SELECT garbage_collected_at FROM harness_retained_blobs
            WHERE workspace_id = ? AND content_sha256 = ?
            """,
            (WORKSPACE_ID, digest(1)),
        ).fetchone()

        assert stored == (collected_at.isoformat(),)
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                """
                UPDATE harness_retained_blobs SET garbage_collected_at = ?
                WHERE workspace_id = ? AND content_sha256 = ?
                """,
                (
                    (collected_at + timedelta(seconds=1)).isoformat(),
                    WORKSPACE_ID,
                    digest(1),
                ),
            )
    finally:
        connection.close()


def test_reachable_blob_and_immutable_evidence_cannot_be_collected(
    tmp_path: Path,
) -> None:
    path = database_path(tmp_path)
    initialize(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        insert_blob(connection)
        reachable_json = f'["{digest(1)}"]'
        connection.execute(
            """
            INSERT INTO harness_synced_snapshots (
                workspace_id, snapshot_sha256, manifest_sha256,
                through_journal_sequence, reachable_json, synced_at
            ) VALUES (?, ?, ?, 0, ?, ?)
            """,
            (
                WORKSPACE_ID,
                digest(50),
                digest(51),
                reachable_json,
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO harness_artifact_tombstones (
                workspace_id, content_sha256, tombstoned_at,
                delete_after, reason
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                WORKSPACE_ID,
                digest(1),
                (NOW + timedelta(days=1)).isoformat(),
                (NOW + timedelta(days=2)).isoformat(),
                "Retention expired.",
            ),
        )

        with pytest.raises(sqlite3.IntegrityError, match="not collectable"):
            connection.execute(
                """
                UPDATE harness_retained_blobs SET garbage_collected_at = ?
                WHERE workspace_id = ? AND content_sha256 = ?
                """,
                (
                    (NOW + timedelta(days=3)).isoformat(),
                    WORKSPACE_ID,
                    digest(1),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM harness_artifact_tombstones")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM harness_retained_blobs")
    finally:
        connection.close()
