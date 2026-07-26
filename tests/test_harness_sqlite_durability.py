import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import (
    AppendRequest,
    AppendStatus,
    GlobalJournalReadRequest,
    JournalReadRequest,
)
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.faults import JournalFaultPoint
from app.services.harness.journal.sqlite import SQLiteEventJournal
from app.services.harness.journal.sqlite_projection_store import (
    SQLiteProjectionStore,
)
from scripts.probe_harness_sqlite_kill import (
    KILL_EXIT_CODE,
    NOW,
    PROJECTION_NAME,
    append_request,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KILL_PROBE = PROJECT_ROOT / "scripts" / "probe_harness_sqlite_kill.py"
PRE_COMMIT_FAULT_POINTS = (
    JournalFaultPoint.AFTER_EVENTS_INSERTED,
    JournalFaultPoint.AFTER_AGGREGATE_UPDATED,
    JournalFaultPoint.AFTER_PROJECTIONS_APPLIED,
    JournalFaultPoint.BEFORE_COMMIT,
)


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def run_kill_probe(path: Path, fault_point: JournalFaultPoint) -> None:
    environment = {"PYTHONPATH": str(PROJECT_ROOT)}
    completed = subprocess.run(
        [
            sys.executable,
            str(KILL_PROBE),
            "--database-path",
            str(path),
            "--fault-point",
            fault_point.value,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == KILL_EXIT_CODE, completed.stderr


@pytest.mark.parametrize("fault_point", PRE_COMMIT_FAULT_POINTS)
def test_process_kill_before_commit_persists_no_event(
    tmp_path: Path,
    fault_point: JournalFaultPoint,
) -> None:
    path = database_path(tmp_path)
    run_kill_probe(path, fault_point)

    async def verify() -> None:
        journal = await SQLiteEventJournal.open(path)
        page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=append_request().workspace_id,
                after_journal_sequence=0,
            )
        )
        store = await SQLiteProjectionStore.open(path)
        projection = await store.load(
            append_request().workspace_id,
            PROJECTION_NAME,
        )
        await store.close()
        await journal.close()
        assert page.events == ()
        assert projection is None

    asyncio.run(verify())


def test_process_kill_after_commit_is_recovered_by_idempotent_replay(
    tmp_path: Path,
) -> None:
    path = database_path(tmp_path)
    run_kill_probe(path, JournalFaultPoint.AFTER_COMMIT)

    async def verify() -> None:
        journal = await SQLiteEventJournal.open(path)
        replay = await journal.append(append_request())
        page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=append_request().workspace_id,
                after_journal_sequence=0,
            )
        )
        store = await SQLiteProjectionStore.open(path)
        projection = await store.load(
            append_request().workspace_id,
            PROJECTION_NAME,
        )
        await store.close()
        await journal.close()

        assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
        assert len(page.events) == 1
        assert page.events[0].event.event_id == append_request().events[0].event_id
        assert projection is not None
        assert projection.checkpoint.state_json == '{"count":1}'

    asyncio.run(verify())


class OneShotDiskFull:
    def __init__(self) -> None:
        self.enabled = False

    def __call__(self, fault_point: JournalFaultPoint) -> None:
        if self.enabled and fault_point is JournalFaultPoint.BEFORE_COMMIT:
            self.enabled = False
            raise sqlite3.OperationalError(
                "database or disk is full: /private/storage/path"
            )


def second_request() -> AppendRequest:
    first = append_request()
    second_event = first.events[0].model_copy(
        update={
            "event_id": "evt_" + "2" * 32,
            "aggregate_sequence": 2,
            "occurred_at": NOW + timedelta(seconds=1),
        }
    )
    return AppendRequest(
        workspace_id=first.workspace_id,
        aggregate_id=first.aggregate_id,
        expected_sequence=1,
        idempotency_key="sqlite-disk-full-command",
        request_sha256="8" * 64,
        durability=first.durability,
        events=(second_event,),
    )


def test_disk_full_failure_preserves_acknowledged_data_and_recovers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        disk_full = OneShotDiskFull()
        journal = await SQLiteEventJournal.open(
            database_path(tmp_path),
            clock=lambda: NOW,
            fault_injector=disk_full,
        )
        first = await journal.append(append_request())
        disk_full.enabled = True
        with pytest.raises(
            JournalStorageError,
            match="journal storage operation failed",
        ) as failure:
            await journal.append(second_request())
        assert "/private/storage/path" not in str(failure.value)

        after_failure = await journal.read_aggregate(
            JournalReadRequest(
                workspace_id=first.workspace_id,
                aggregate_id=first.aggregate_id,
                after_sequence=0,
            )
        )
        recovered = await journal.append(second_request())
        after_recovery = await journal.read_aggregate(
            JournalReadRequest(
                workspace_id=first.workspace_id,
                aggregate_id=first.aggregate_id,
                after_sequence=0,
            )
        )
        await journal.close()

        assert tuple(event.aggregate_sequence for event in after_failure.events) == (1,)
        assert recovered.status is AppendStatus.APPENDED
        assert tuple(event.aggregate_sequence for event in after_recovery.events) == (
            1,
            2,
        )

    asyncio.run(scenario())
