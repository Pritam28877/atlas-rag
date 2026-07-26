import asyncio
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import SQLiteEventJournal
from scripts.probe_harness_sqlite_kill import NOW

WORKSPACE_ID = "wsp_" + "0" * 32


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def initialize(path: Path) -> None:
    async def scenario() -> None:
        journal = await SQLiteEventJournal.open(path)
        await journal.close()

    asyncio.run(scenario())


def test_reservation_transitions_maintain_usage_counters(
    tmp_path: Path,
) -> None:
    path = database_path(tmp_path)
    initialize(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO harness_workspace_storage (
                workspace_id, updated_at
            ) VALUES (?, ?)
            """,
            (WORKSPACE_ID, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO harness_blob_reservations (
                workspace_id, content_sha256, size_bytes,
                reservation_status, created_at, updated_at
            ) VALUES (?, ?, 4096, 'reserved', ?, ?)
            """,
            (WORKSPACE_ID, "a" * 64, NOW.isoformat(), NOW.isoformat()),
        )
        reserved = connection.execute(
            """
            SELECT used_blob_bytes, reserved_blob_bytes
            FROM harness_workspace_storage WHERE workspace_id = ?
            """,
            (WORKSPACE_ID,),
        ).fetchone()
        committed_at = (NOW + timedelta(seconds=1)).isoformat()
        connection.execute(
            """
            UPDATE harness_blob_reservations
            SET reservation_status = 'committed', updated_at = ?
            WHERE workspace_id = ? AND content_sha256 = ?
            """,
            (committed_at, WORKSPACE_ID, "a" * 64),
        )
        committed = connection.execute(
            """
            SELECT used_blob_bytes, reserved_blob_bytes
            FROM harness_workspace_storage WHERE workspace_id = ?
            """,
            (WORKSPACE_ID,),
        ).fetchone()

        assert reserved == (0, 4096)
        assert committed == (4096, 0)
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                """
                UPDATE harness_blob_reservations
                SET reservation_status = 'released', updated_at = ?
                WHERE workspace_id = ? AND content_sha256 = ?
                """,
                (
                    (NOW + timedelta(seconds=2)).isoformat(),
                    WORKSPACE_ID,
                    "a" * 64,
                ),
            )
    finally:
        connection.close()


def test_released_reservation_can_retry_and_seal_cannot_reverse(
    tmp_path: Path,
) -> None:
    path = database_path(tmp_path)
    initialize(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO harness_workspace_storage (
                workspace_id, updated_at
            ) VALUES (?, ?)
            """,
            (WORKSPACE_ID, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO harness_blob_reservations (
                workspace_id, content_sha256, size_bytes,
                reservation_status, created_at, updated_at
            ) VALUES (?, ?, 1024, 'reserved', ?, ?)
            """,
            (WORKSPACE_ID, "b" * 64, NOW.isoformat(), NOW.isoformat()),
        )
        released_at = (NOW + timedelta(seconds=1)).isoformat()
        connection.execute(
            """
            UPDATE harness_blob_reservations
            SET reservation_status = 'released', updated_at = ?
            WHERE workspace_id = ? AND content_sha256 = ?
            """,
            (released_at, WORKSPACE_ID, "b" * 64),
        )
        retried_at = (NOW + timedelta(seconds=2)).isoformat()
        connection.execute(
            """
            UPDATE harness_blob_reservations
            SET reservation_status = 'reserved', updated_at = ?
            WHERE workspace_id = ? AND content_sha256 = ?
            """,
            (retried_at, WORKSPACE_ID, "b" * 64),
        )
        connection.execute(
            """
            UPDATE harness_workspace_storage
            SET sealed = 1, seal_reason = 'disk_reserve', updated_at = ?
            WHERE workspace_id = ?
            """,
            (retried_at, WORKSPACE_ID),
        )
        counters = connection.execute(
            """
            SELECT reserved_blob_bytes, sealed, seal_reason
            FROM harness_workspace_storage WHERE workspace_id = ?
            """,
            (WORKSPACE_ID,),
        ).fetchone()

        assert counters == (1024, 1, "disk_reserve")
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                """
                UPDATE harness_workspace_storage
                SET sealed = 0, seal_reason = NULL, updated_at = ?
                WHERE workspace_id = ?
                """,
                (
                    (NOW + timedelta(seconds=3)).isoformat(),
                    WORKSPACE_ID,
                ),
            )
    finally:
        connection.close()
