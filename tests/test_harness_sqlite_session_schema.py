import asyncio
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from app.services.harness.journal import SQLiteEventJournal

WORKSPACE_ID = "wsp_" + "1" * 32
PRINCIPAL_ID = "prn_" + "2" * 32
SUBSCRIPTION_ID = "sub_" + "3" * 32
IDEMPOTENCY_KEY = "turn-command-0001"
RESULT_JSON = '{"kind":"inline_text"}'


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def initialize_database(path: Path, schema_version: int | None = None) -> None:
    if schema_version is None:
        asyncio.run(_initialize_latest(path))
        return
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE harness_journal_schema (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL CHECK (schema_version >= 1)
            );
            INSERT INTO harness_journal_schema VALUES (1, {schema_version});
            """
        )
    finally:
        connection.close()
    os.chmod(path, 0o600)


async def _initialize_latest(path: Path) -> None:
    journal = await SQLiteEventJournal.open(path)
    await journal.close()


def insert_command_receipt(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO harness_command_receipts (
            workspace_id, principal_id, idempotency_key, command_kind,
            request_sha256, response_kind, result_json, result_sha256,
            committed_at
        ) VALUES (?, ?, ?, 'turn.start', ?, 'turn_status', ?, ?, ?)
        """,
        (
            WORKSPACE_ID,
            PRINCIPAL_ID,
            IDEMPOTENCY_KEY,
            "a" * 64,
            RESULT_JSON,
            hashlib.sha256(RESULT_JSON.encode()).hexdigest(),
            "2026-07-27T12:00:00+00:00",
        ),
    )


def insert_subscription_cursor(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO harness_subscription_cursors (
            workspace_id, principal_id, subscription_id,
            acknowledged_sequence, delivered_sequence, updated_at, expires_at
        ) VALUES (?, ?, ?, 3, 5, ?, ?)
        """,
        (
            WORKSPACE_ID,
            PRINCIPAL_ID,
            SUBSCRIPTION_ID,
            "2026-07-27T12:00:00+00:00",
            "2026-08-03T12:00:00+00:00",
        ),
    )


def test_v6_database_migrates_to_session_schema(tmp_path: Path) -> None:
    path = database_path(tmp_path)
    initialize_database(path, schema_version=6)
    asyncio.run(_initialize_latest(path))

    connection = sqlite3.connect(path)
    try:
        version = connection.execute(
            "SELECT schema_version FROM harness_journal_schema"
        ).fetchone()
        tables = connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table'
              AND name IN (
                  'harness_command_receipts',
                  'harness_subscription_cursors'
              )
            """
        ).fetchone()
    finally:
        connection.close()

    assert version == (7,)
    assert tables == (2,)


def test_command_receipts_are_bounded_and_immutable(tmp_path: Path) -> None:
    path = database_path(tmp_path)
    initialize_database(path)
    connection = sqlite3.connect(path)
    try:
        insert_command_receipt(connection)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE harness_command_receipts SET result_sha256 = ?
                WHERE workspace_id = ? AND principal_id = ?
                """,
                ("b" * 64, WORKSPACE_ID, PRINCIPAL_ID),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                DELETE FROM harness_command_receipts
                WHERE workspace_id = ? AND principal_id = ?
                """,
                (WORKSPACE_ID, PRINCIPAL_ID),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO harness_command_receipts (
                    workspace_id, principal_id, idempotency_key, command_kind,
                    request_sha256, response_kind, result_json, result_sha256,
                    committed_at
                ) VALUES (?, ?, 'too-short', 'turn.start', ?, 'turn_status',
                          '{}', ?, ?)
                """,
                (
                    WORKSPACE_ID,
                    PRINCIPAL_ID,
                    "a" * 64,
                    hashlib.sha256(b"{}").hexdigest(),
                    "2026-07-27T12:00:00+00:00",
                ),
            )
    finally:
        connection.close()


def test_subscription_cursors_are_monotonic_and_bounded(tmp_path: Path) -> None:
    path = database_path(tmp_path)
    initialize_database(path)
    connection = sqlite3.connect(path)
    try:
        insert_subscription_cursor(connection)
        connection.execute(
            """
            UPDATE harness_subscription_cursors
            SET generation = 2, acknowledged_sequence = 5,
                delivered_sequence = 8,
                updated_at = '2026-07-27T12:01:00+00:00',
                expires_at = '2026-08-03T12:01:00+00:00'
            WHERE workspace_id = ? AND principal_id = ?
              AND subscription_id = ?
            """,
            (WORKSPACE_ID, PRINCIPAL_ID, SUBSCRIPTION_ID),
        )
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            connection.execute(
                """
                UPDATE harness_subscription_cursors
                SET generation = 3, acknowledged_sequence = 4
                WHERE workspace_id = ? AND principal_id = ?
                  AND subscription_id = ?
                """,
                (WORKSPACE_ID, PRINCIPAL_ID, SUBSCRIPTION_ID),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                INSERT INTO harness_subscription_cursors (
                    workspace_id, principal_id, subscription_id,
                    acknowledged_sequence, delivered_sequence,
                    updated_at, expires_at
                ) VALUES (?, ?, ?, 2, 1, ?, ?)
                """,
                (
                    WORKSPACE_ID,
                    PRINCIPAL_ID,
                    "sub_" + "4" * 32,
                    "2026-07-27T12:00:00+00:00",
                    "2026-08-03T12:00:00+00:00",
                ),
            )
    finally:
        connection.close()


def test_session_keys_cannot_be_reowned(tmp_path: Path) -> None:
    path = database_path(tmp_path)
    initialize_database(path)
    connection = sqlite3.connect(path)
    try:
        insert_command_receipt(connection)
        insert_subscription_cursor(connection)
        other_principal_id = "prn_" + "9" * 32
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            connection.execute(
                """
                INSERT INTO harness_command_receipts (
                    workspace_id, principal_id, idempotency_key, command_kind,
                    request_sha256, response_kind, result_json, result_sha256,
                    committed_at
                )
                SELECT workspace_id, ?, idempotency_key, command_kind,
                       request_sha256, response_kind, result_json,
                       result_sha256, committed_at
                FROM harness_command_receipts
                """,
                (other_principal_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            connection.execute(
                """
                INSERT INTO harness_subscription_cursors (
                    workspace_id, principal_id, subscription_id, generation,
                    acknowledged_sequence, delivered_sequence,
                    updated_at, expires_at
                )
                SELECT workspace_id, ?, subscription_id, generation,
                       acknowledged_sequence, delivered_sequence,
                       updated_at, expires_at
                FROM harness_subscription_cursors
                """,
                (other_principal_id,),
            )
    finally:
        connection.close()
