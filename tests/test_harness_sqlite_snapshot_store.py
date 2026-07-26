import asyncio
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.artifacts import (
    SealedJournalSegment,
    SyncedSnapshot,
    SyncedSnapshotBundle,
)
from app.services.harness.journal import (
    AppendRequest,
    SnapshotStoreConflict,
    SQLiteEventJournal,
    SQLiteSnapshotStore,
)
from scripts.probe_harness_sqlite_kill import NOW, append_request

WORKSPACE_ID = "wsp_" + "0" * 32


def digest(number: int) -> str:
    return f"{number:064x}"


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def bundle(
    *,
    through_sequence: int,
    event_count: int | None = None,
    snapshot_number: int = 100,
) -> SyncedSnapshotBundle:
    snapshot = SyncedSnapshot(
        workspace_id=WORKSPACE_ID,
        snapshot_sha256=digest(snapshot_number),
        manifest_sha256=digest(snapshot_number + 1),
        through_journal_sequence=through_sequence,
        reachable_content_sha256s=(digest(1), digest(2)),
        synced_at=NOW + timedelta(minutes=2),
    )
    if event_count is None:
        return SyncedSnapshotBundle(snapshot=snapshot, segments=())
    segment = SealedJournalSegment(
        workspace_id=WORKSPACE_ID,
        segment_sha256=digest(snapshot_number + 2),
        snapshot_sha256=snapshot.snapshot_sha256,
        first_journal_sequence=1,
        last_journal_sequence=through_sequence,
        event_count=event_count,
        sealed_at=NOW + timedelta(minutes=1),
    )
    return SyncedSnapshotBundle(snapshot=snapshot, segments=(segment,))


def two_event_request() -> AppendRequest:
    request = append_request()
    second_event = request.events[0].model_copy(
        update={
            "event_id": "evt_" + "2" * 32,
            "aggregate_sequence": 2,
            "occurred_at": NOW + timedelta(seconds=1),
        }
    )
    return request.model_copy(update={"events": (*request.events, second_event)})


def test_snapshot_and_segment_restore_after_reopen(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        journal = await SQLiteEventJournal.open(path, clock=lambda: NOW)
        await journal.append(two_event_request())
        await journal.close()

        expected = bundle(through_sequence=2, event_count=2)
        store = await SQLiteSnapshotStore.open(path)
        first = await store.save(expected)
        replay = await store.save(expected)
        await store.close()

        reopened = await SQLiteSnapshotStore.open(path)
        restored = await reopened.load_latest(WORKSPACE_ID)
        by_digest = await reopened.load(
            WORKSPACE_ID,
            expected.snapshot.snapshot_sha256,
        )
        await reopened.close()

        assert first == expected
        assert replay == expected
        assert restored == expected
        assert by_digest == expected

    asyncio.run(scenario())


def test_empty_snapshot_is_a_durable_replay_baseline(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        store = await SQLiteSnapshotStore.open(path)
        expected = bundle(through_sequence=0)

        stored = await store.save(expected)
        await store.close()

        assert stored == expected

    asyncio.run(scenario())


def test_snapshot_rejects_uncommitted_or_mismatched_segment(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        journal = await SQLiteEventJournal.open(path, clock=lambda: NOW)
        await journal.append(two_event_request())
        await journal.close()
        store = await SQLiteSnapshotStore.open(path)

        with pytest.raises(SnapshotStoreConflict, match="segment"):
            await store.save(bundle(through_sequence=2, event_count=1))
        with pytest.raises(SnapshotStoreConflict, match="not durable"):
            await store.save(
                bundle(
                    through_sequence=3,
                    event_count=3,
                    snapshot_number=200,
                )
            )
        missing = await store.load_latest(WORKSPACE_ID)
        await store.close()

        assert missing is None

    asyncio.run(scenario())


def test_snapshot_digest_conflict_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        store = await SQLiteSnapshotStore.open(path)
        original = bundle(through_sequence=0)
        await store.save(original)
        conflicting = SyncedSnapshotBundle(
            snapshot=original.snapshot.model_copy(
                update={"manifest_sha256": digest(999)}
            ),
            segments=(),
        )

        with pytest.raises(SnapshotStoreConflict, match="different facts"):
            await store.save(conflicting)
        restored = await store.load_latest(WORKSPACE_ID)
        await store.close()

        assert restored == original

    asyncio.run(scenario())


def test_snapshot_tables_reject_update_and_delete(tmp_path: Path) -> None:
    async def seed(path: Path) -> None:
        store = await SQLiteSnapshotStore.open(path)
        await store.save(bundle(through_sequence=0))
        await store.close()

    path = database_path(tmp_path)
    asyncio.run(seed(path))
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE harness_synced_snapshots
                SET manifest_sha256 = ?
                """,
                (digest(500),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM harness_synced_snapshots")
    finally:
        connection.close()
