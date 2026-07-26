import asyncio
import hashlib
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

from app.services.harness.journal import (
    AppendRequest,
    AppendStatus,
    GlobalJournalReadRequest,
    SQLiteEventJournal,
)
from app.services.harness.journal.receipts import build_append_result
from app.services.harness.journal.sqlite_migrations import (
    SQLITE_MIGRATE_V1_TO_V2,
    SQLITE_MIGRATE_V2_TO_V3,
    SQLITE_MIGRATE_V3_TO_V4,
    SQLITE_MIGRATE_V4_TO_V5,
)
from scripts.probe_harness_sqlite_kill import NOW, append_request

SECOND_WORKSPACE_ID = "wsp_" + "2" * 32

V1_SCHEMA = """
CREATE TABLE harness_journal_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
);
INSERT INTO harness_journal_schema VALUES (1, 1);

CREATE TABLE harness_aggregates (
    workspace_id TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    current_sequence INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, aggregate_id)
);

CREATE TABLE harness_events (
    journal_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_sequence INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    durability TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id, aggregate_id)
        REFERENCES harness_aggregates(workspace_id, aggregate_id),
    UNIQUE (workspace_id, event_id),
    UNIQUE (workspace_id, aggregate_id, aggregate_sequence)
);

CREATE TABLE harness_idempotency (
    workspace_id TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (workspace_id, aggregate_id, idempotency_key),
    FOREIGN KEY (workspace_id, aggregate_id)
        REFERENCES harness_aggregates(workspace_id, aggregate_id)
);
"""


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def create_v1_database(path: Path) -> tuple[AppendRequest, AppendRequest]:
    first_request = append_request()
    second_request = first_request.model_copy(
        update={
            "workspace_id": SECOND_WORKSPACE_ID,
            "idempotency_key": "sqlite-v1-second-workspace",
            "request_sha256": "7" * 64,
        }
    )
    first_event_json = first_request.events[0].model_dump_json()
    second_event_json = second_request.events[0].model_dump_json()
    first_result = build_append_result(first_request, NOW, (1,))

    connection = sqlite3.connect(path)
    try:
        connection.executescript(V1_SCHEMA)
        connection.executemany(
            """
            INSERT INTO harness_aggregates (
                workspace_id, aggregate_id, current_sequence
            ) VALUES (?, ?, 1)
            """,
            (
                (first_request.workspace_id, first_request.aggregate_id),
                (second_request.workspace_id, second_request.aggregate_id),
            ),
        )
        connection.executemany(
            """
            INSERT INTO harness_events (
                event_id, workspace_id, aggregate_id, aggregate_sequence,
                event_json, event_sha256, request_sha256, durability,
                committed_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                (
                    first_request.events[0].event_id,
                    first_request.workspace_id,
                    first_request.aggregate_id,
                    first_event_json,
                    hashlib.sha256(first_event_json.encode()).hexdigest(),
                    first_request.request_sha256,
                    first_request.durability.value,
                    NOW.isoformat(),
                ),
                (
                    second_request.events[0].event_id,
                    second_request.workspace_id,
                    second_request.aggregate_id,
                    second_event_json,
                    hashlib.sha256(second_event_json.encode()).hexdigest(),
                    second_request.request_sha256,
                    second_request.durability.value,
                    NOW.isoformat(),
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO harness_idempotency (
                workspace_id, aggregate_id, idempotency_key,
                request_sha256, result_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                first_request.workspace_id,
                first_request.aggregate_id,
                first_request.idempotency_key,
                first_request.request_sha256,
                first_result.model_dump_json(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)
    return first_request, second_request


def next_request(request: AppendRequest, event_number: int) -> AppendRequest:
    next_event = request.events[0].model_copy(
        update={
            "event_id": f"evt_{event_number:032x}",
            "aggregate_sequence": 2,
            "occurred_at": NOW + timedelta(seconds=1),
        }
    )
    return request.model_copy(
        update={
            "expected_sequence": 1,
            "idempotency_key": f"sqlite-v2-command-{event_number:04d}",
            "request_sha256": f"{event_number:x}" * 64,
            "events": (next_event,),
        }
    )


def test_v1_migration_preserves_facts_and_uses_workspace_positions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        first_request, second_request = create_v1_database(path)
        journal = await SQLiteEventJournal.open(path)

        replay = await journal.append(first_request)
        first_append = await journal.append(next_request(first_request, 3))
        second_append = await journal.append(next_request(second_request, 4))
        first_page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=first_request.workspace_id,
                after_journal_sequence=0,
            )
        )
        second_page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=second_request.workspace_id,
                after_journal_sequence=0,
            )
        )
        await journal.close()

        connection = sqlite3.connect(path)
        try:
            schema_version = connection.execute(
                "SELECT schema_version FROM harness_journal_schema"
            ).fetchone()
            primary_key = connection.execute(
                "PRAGMA table_info(harness_events)"
            ).fetchall()
        finally:
            connection.close()

        assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
        assert replay.journal_sequences == (1,)
        assert first_append.journal_sequences == (2,)
        assert second_append.journal_sequences == (3,)
        assert tuple(event.journal_sequence for event in first_page.events) == (
            1,
            2,
        )
        assert tuple(event.journal_sequence for event in second_page.events) == (
            2,
            3,
        )
        assert schema_version == (7,)
        key_columns = {
            row[1]: row[5]
            for row in primary_key
            if row[5] > 0
        }
        assert key_columns == {"workspace_id": 1, "journal_sequence": 2}

    asyncio.run(scenario())


def test_v2_migration_preserves_journal_and_adds_snapshot_schema(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        first_request, _ = create_v1_database(path)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SQLITE_MIGRATE_V1_TO_V2)
        finally:
            connection.close()
        os.chmod(path, 0o600)

        journal = await SQLiteEventJournal.open(path)
        replay = await journal.append(first_request)
        await journal.close()

        migrated = sqlite3.connect(path)
        try:
            schema_version = migrated.execute(
                "SELECT schema_version FROM harness_journal_schema"
            ).fetchone()
            event_count = migrated.execute(
                "SELECT COUNT(*) FROM harness_events"
            ).fetchone()
            retention_tables = migrated.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                      'harness_synced_snapshots',
                      'harness_sealed_segments'
                  )
                """
            ).fetchone()
        finally:
            migrated.close()

        assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
        assert schema_version == (7,)
        assert event_count == (2,)
        assert retention_tables == (2,)

    asyncio.run(scenario())


def test_v3_migration_preserves_snapshots_and_adds_retention_evidence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        create_v1_database(path)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SQLITE_MIGRATE_V1_TO_V2)
            connection.executescript(SQLITE_MIGRATE_V2_TO_V3)
            connection.execute(
                """
                INSERT INTO harness_synced_snapshots (
                    workspace_id, snapshot_sha256, manifest_sha256,
                    through_journal_sequence, reachable_json, synced_at
                ) VALUES (?, ?, ?, 1, '[]', ?)
                """,
                (
                    "wsp_" + "0" * 32,
                    "a" * 64,
                    "b" * 64,
                    NOW.isoformat(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(path, 0o600)

        journal = await SQLiteEventJournal.open(path)
        await journal.close()

        migrated = sqlite3.connect(path)
        try:
            schema_version = migrated.execute(
                "SELECT schema_version FROM harness_journal_schema"
            ).fetchone()
            snapshot_count = migrated.execute(
                "SELECT COUNT(*) FROM harness_synced_snapshots"
            ).fetchone()
            evidence_tables = migrated.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                      'harness_retained_blobs',
                      'harness_artifact_references',
                      'harness_artifact_tombstones',
                      'harness_artifact_legal_holds'
                  )
                """
            ).fetchone()
        finally:
            migrated.close()

        assert schema_version == (7,)
        assert snapshot_count == (1,)
        assert evidence_tables == (4,)

    asyncio.run(scenario())


def test_v4_migration_adds_storage_reservations(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        create_v1_database(path)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SQLITE_MIGRATE_V1_TO_V2)
            connection.executescript(SQLITE_MIGRATE_V2_TO_V3)
            connection.executescript(SQLITE_MIGRATE_V3_TO_V4)
        finally:
            connection.close()
        os.chmod(path, 0o600)

        journal = await SQLiteEventJournal.open(path)
        await journal.close()

        migrated = sqlite3.connect(path)
        try:
            version = migrated.execute(
                "SELECT schema_version FROM harness_journal_schema"
            ).fetchone()
            tables = migrated.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                      'harness_workspace_storage',
                      'harness_blob_reservations'
                  )
                """
            ).fetchone()
        finally:
            migrated.close()

        assert version == (7,)
        assert tables == (2,)

    asyncio.run(scenario())


def test_v5_migration_adds_recovery_facts(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        create_v1_database(path)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SQLITE_MIGRATE_V1_TO_V2)
            connection.executescript(SQLITE_MIGRATE_V2_TO_V3)
            connection.executescript(SQLITE_MIGRATE_V3_TO_V4)
            connection.executescript(SQLITE_MIGRATE_V4_TO_V5)
        finally:
            connection.close()
        os.chmod(path, 0o600)

        journal = await SQLiteEventJournal.open(path)
        await journal.close()

        migrated = sqlite3.connect(path)
        try:
            version = migrated.execute(
                "SELECT schema_version FROM harness_journal_schema"
            ).fetchone()
            tables = migrated.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                      'harness_recovery_operations',
                      'harness_recovery_leases'
                  )
                """
            ).fetchone()
        finally:
            migrated.close()

        assert version == (7,)
        assert tables == (2,)

    asyncio.run(scenario())
