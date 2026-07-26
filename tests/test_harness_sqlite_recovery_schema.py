import asyncio
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import SQLiteEventJournal
from scripts.probe_harness_sqlite_kill import NOW

WORKSPACE_ID = "wsp_" + "0" * 32
OPERATION_ID = "opn_" + "1" * 32


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def initialize(path: Path) -> None:
    async def scenario() -> None:
        journal = await SQLiteEventJournal.open(path)
        await journal.close()

    asyncio.run(scenario())


def operation_json(state: str) -> str:
    return json.dumps(
        {
            "idempotency_class": "non_idempotent",
            "operation_id": OPERATION_ID,
            "state": state,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def test_operation_recovery_transition_is_fail_closed(tmp_path: Path) -> None:
    path = database_path(tmp_path)
    initialize(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO harness_recovery_operations (
                workspace_id, operation_id, operation_state,
                idempotency_class, operation_json, updated_at
            ) VALUES (?, ?, 'dispatched', 'non_idempotent', ?, ?)
            """,
            (
                WORKSPACE_ID,
                OPERATION_ID,
                operation_json("dispatched"),
                NOW.isoformat(),
            ),
        )
        recovered_at = (NOW + timedelta(seconds=1)).isoformat()
        connection.execute(
            """
            UPDATE harness_recovery_operations
            SET operation_state = 'ambiguous',
                operation_json = ?,
                updated_at = ?
            WHERE workspace_id = ? AND operation_id = ?
            """,
            (
                operation_json("ambiguous"),
                recovered_at,
                WORKSPACE_ID,
                OPERATION_ID,
            ),
        )

        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                """
                UPDATE harness_recovery_operations
                SET operation_state = 'dispatched',
                    operation_json = ?,
                    updated_at = ?
                WHERE workspace_id = ? AND operation_id = ?
                """,
                (
                    operation_json("dispatched"),
                    (NOW + timedelta(seconds=2)).isoformat(),
                    WORKSPACE_ID,
                    OPERATION_ID,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM harness_recovery_operations")
    finally:
        connection.close()


def test_fencing_lease_expiry_is_monotonic_and_immutable(
    tmp_path: Path,
) -> None:
    path = database_path(tmp_path)
    initialize(path)
    connection = sqlite3.connect(path)
    expires_at = NOW + timedelta(seconds=1)
    expired_at = NOW + timedelta(seconds=2)
    try:
        connection.execute(
            """
            INSERT INTO harness_recovery_leases (
                workspace_id, lease_sha256, fencing_generation,
                expires_at, lease_state, updated_at
            ) VALUES (?, ?, 7, ?, 'active', ?)
            """,
            (
                WORKSPACE_ID,
                "a" * 64,
                expires_at.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            UPDATE harness_recovery_leases
            SET lease_state = 'expired', expired_at = ?, updated_at = ?
            WHERE workspace_id = ? AND lease_sha256 = ?
            """,
            (
                expired_at.isoformat(),
                expired_at.isoformat(),
                WORKSPACE_ID,
                "a" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                """
                UPDATE harness_recovery_leases
                SET lease_state = 'active', expired_at = NULL, updated_at = ?
                WHERE workspace_id = ? AND lease_sha256 = ?
                """,
                (
                    (NOW + timedelta(seconds=3)).isoformat(),
                    WORKSPACE_ID,
                    "a" * 64,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM harness_recovery_leases")
    finally:
        connection.close()
