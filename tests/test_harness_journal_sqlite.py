import asyncio
import hashlib
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import (
    AppendRequest,
    AppendStatus,
    GlobalJournalReadRequest,
    JournalBusyError,
    JournalConflictCode,
    JournalConflictError,
    JournalDurability,
    JournalReadRequest,
    SQLiteEventJournal,
)
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    TraceLink,
)

NOW = datetime(2026, 7, 26, 19, 0, tzinfo=UTC)


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


WORKSPACE_ID = identifier("wsp")


def event(
    sequence: int,
    *,
    event_number: int | None = None,
) -> EventRecord:
    text = f"event-{sequence}"
    encoded = text.encode()
    return EventRecord(
        event_id=identifier(
            "evt",
            sequence if event_number is None else event_number,
        ),
        event_type="Turn.Accepted",
        schema_version="1.2",
        aggregate_id=identifier("trn"),
        aggregate_sequence=sequence,
        actor_kind=EventActorKind.SYSTEM,
        actor_principal_id=identifier("prn"),
        occurred_at=NOW + timedelta(seconds=sequence),
        trace=TraceLink(
            request_id=identifier("req"),
            correlation_id=identifier("evt", 99),
        ),
        payload=InlinePayload(
            text=text,
            size_bytes=len(encoded),
            content_sha256=hashlib.sha256(encoded).hexdigest(),
        ),
    )


def append_request(
    *events: EventRecord,
    expected_sequence: int = 0,
    key_number: int = 1,
    digest_number: int = 1,
    durability: JournalDurability = JournalDurability.SYNCHRONOUS,
    workspace_id: str = WORKSPACE_ID,
) -> AppendRequest:
    return AppendRequest(
        workspace_id=workspace_id,
        aggregate_id=identifier("trn"),
        expected_sequence=expected_sequence,
        idempotency_key=f"journal-command-{key_number:04d}",
        request_sha256=f"{digest_number:x}" * 64,
        durability=durability,
        events=events,
    )


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def test_append_replay_pagination_and_reopen_are_stable(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        journal = await SQLiteEventJournal.open(path, clock=lambda: NOW)
        request = append_request(event(1), event(2), event(3))

        result = await journal.append(request)
        replay = await journal.append(request)
        first_page = await journal.read_aggregate(
            JournalReadRequest(
                workspace_id=WORKSPACE_ID,
                aggregate_id=identifier("trn"),
                after_sequence=0,
                limit=2,
            )
        )
        second_page = await journal.read_aggregate(
            JournalReadRequest(
                workspace_id=WORKSPACE_ID,
                aggregate_id=identifier("trn"),
                after_sequence=2,
                limit=2,
            )
        )
        global_page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=WORKSPACE_ID,
                after_journal_sequence=0,
                limit=2,
            )
        )
        other_workspace_page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=identifier("wsp", 2),
                after_journal_sequence=0,
            )
        )
        await journal.close()

        assert result.status is AppendStatus.APPENDED
        assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
        assert replay.receipt_sha256 == result.receipt_sha256
        assert replay.committed_at == result.committed_at
        assert result.journal_sequences == (1, 2, 3)
        assert tuple(item.aggregate_sequence for item in first_page.events) == (
            1,
            2,
        )
        assert first_page.has_more
        assert tuple(item.aggregate_sequence for item in second_page.events) == (3,)
        assert not second_page.has_more
        assert tuple(
            item.journal_sequence for item in global_page.events
        ) == (1, 2)
        assert global_page.has_more
        assert other_workspace_page.events == ()
        assert path.stat().st_mode & 0o077 == 0

        reopened = await SQLiteEventJournal.open(path)
        restored = await reopened.read_aggregate(
            JournalReadRequest(
                workspace_id=WORKSPACE_ID,
                aggregate_id=identifier("trn"),
                after_sequence=0,
            )
        )
        await reopened.close()
        assert restored.events == request.events

    asyncio.run(scenario())


def test_idempotency_mismatch_and_stale_sequence_leave_no_gap(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        journal = await SQLiteEventJournal.open(database_path(tmp_path))
        await journal.append(append_request(event(1)))

        with pytest.raises(JournalConflictError) as mismatch:
            await journal.append(
                append_request(event(1), digest_number=2)
            )
        assert mismatch.value.code is JournalConflictCode.IDEMPOTENCY_MISMATCH
        assert mismatch.value.current_sequence == 1

        with pytest.raises(JournalConflictError) as stale:
            await journal.append(
                append_request(event(1, event_number=2), key_number=2)
            )
        assert stale.value.code is JournalConflictCode.EXPECTED_SEQUENCE
        assert stale.value.current_sequence == 1

        await journal.append(
            append_request(
                event(2),
                expected_sequence=1,
                key_number=3,
                digest_number=3,
            )
        )
        page = await journal.read_aggregate(
            JournalReadRequest(
                workspace_id=WORKSPACE_ID,
                aggregate_id=identifier("trn"),
                after_sequence=0,
            )
        )
        await journal.close()
        assert tuple(item.aggregate_sequence for item in page.events) == (1, 2)

    asyncio.run(scenario())


def test_identifiers_and_sequences_are_isolated_by_workspace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        other_workspace_id = identifier("wsp", 2)
        journal = await SQLiteEventJournal.open(database_path(tmp_path))
        first_result = await journal.append(append_request(event(1)))
        second_result = await journal.append(
            append_request(
                event(1),
                workspace_id=other_workspace_id,
            )
        )
        first_page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=WORKSPACE_ID,
                after_journal_sequence=0,
            )
        )
        second_page = await journal.read_global(
            GlobalJournalReadRequest(
                workspace_id=other_workspace_id,
                after_journal_sequence=0,
            )
        )
        await journal.close()

        assert first_result.last_sequence == 1
        assert second_result.last_sequence == 1
        assert first_result.journal_sequences == (1,)
        assert second_result.journal_sequences == (1,)
        assert len(first_page.events) == 1
        assert len(second_page.events) == 1
        assert first_page.events[0].event == second_page.events[0].event
        assert first_page.events[0].journal_sequence == 1
        assert second_page.events[0].journal_sequence == 1

    asyncio.run(scenario())


def test_concurrent_same_sequence_has_one_winner(tmp_path: Path) -> None:
    async def scenario() -> None:
        journal = await SQLiteEventJournal.open(database_path(tmp_path))
        outcomes = await asyncio.gather(
            journal.append(append_request(event(1), key_number=1)),
            journal.append(
                append_request(
                    event(1, event_number=2),
                    key_number=2,
                    digest_number=2,
                )
            ),
            return_exceptions=True,
        )
        page = await journal.read_aggregate(
            JournalReadRequest(
                workspace_id=WORKSPACE_ID,
                aggregate_id=identifier("trn"),
                after_sequence=0,
            )
        )
        await journal.close()

        appended = [
            outcome
            for outcome in outcomes
            if not isinstance(outcome, BaseException)
        ]
        conflicts = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, JournalConflictError)
        ]
        assert len(appended) == 1
        assert len(conflicts) == 1
        assert conflicts[0].code is JournalConflictCode.EXPECTED_SEQUENCE
        assert len(page.events) == 1
        assert page.events[0].aggregate_sequence == 1

    asyncio.run(scenario())


def test_pending_operation_queue_rejects_instead_of_growing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock_entered = threading.Event()
        release_clock = threading.Event()

        def blocking_clock() -> datetime:
            clock_entered.set()
            if not release_clock.wait(timeout=1):
                raise RuntimeError("test clock was not released")
            return NOW

        journal = await SQLiteEventJournal.open(
            database_path(tmp_path),
            maximum_pending_operations=1,
            clock=blocking_clock,
        )
        first_append = asyncio.create_task(journal.append(append_request(event(1))))
        assert await asyncio.to_thread(clock_entered.wait, 1)

        with pytest.raises(JournalBusyError):
            await journal.read_aggregate(
                JournalReadRequest(
                    workspace_id=WORKSPACE_ID,
                    aggregate_id=identifier("trn"),
                    after_sequence=0,
                )
            )
        release_clock.set()
        await first_append
        await journal.close()

    asyncio.run(scenario())


def test_cancellation_does_not_release_worker_capacity_early(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock_entered = threading.Event()
        release_clock = threading.Event()
        clock_completed = threading.Event()
        path = database_path(tmp_path)

        def blocking_clock() -> datetime:
            clock_entered.set()
            if not release_clock.wait(timeout=1):
                raise RuntimeError("test clock was not released")
            clock_completed.set()
            return NOW

        journal = await SQLiteEventJournal.open(
            path,
            maximum_pending_operations=1,
            clock=blocking_clock,
        )
        append_task = asyncio.create_task(
            journal.append(append_request(event(1)))
        )
        assert await asyncio.to_thread(clock_entered.wait, 1)
        append_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await append_task

        with pytest.raises(JournalBusyError):
            await journal.read_aggregate(
                JournalReadRequest(
                    workspace_id=WORKSPACE_ID,
                    aggregate_id=identifier("trn"),
                    after_sequence=0,
                )
            )

        release_clock.set()
        assert await asyncio.to_thread(clock_completed.wait, 1)
        await journal.close()

        connection = sqlite3.connect(path)
        try:
            persisted_events = connection.execute(
                "SELECT COUNT(*) FROM harness_events"
            ).fetchone()
        finally:
            connection.close()
        assert persisted_events == (1,)

    asyncio.run(scenario())


def test_schema_triggers_reject_fact_mutation(tmp_path: Path) -> None:
    path = database_path(tmp_path)

    async def create() -> None:
        journal = await SQLiteEventJournal.open(path)
        await journal.append(append_request(event(1)))
        await journal.close()

    asyncio.run(create())
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE harness_events SET event_json = '{}' WHERE event_id = ?",
                (identifier("evt", 1),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM harness_idempotency")
    finally:
        connection.close()
