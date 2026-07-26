import asyncio
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.journal import (
    AppendRequest,
    JournalCorruptionCode,
    JournalCorruptionError,
    JournalHealthStatus,
    JournalStorageError,
    SQLiteEventJournal,
    SQLiteJournalIntegrityVerifier,
)
from app.services.harness.journal.health import (
    JournalHealthRecord,
    JournalVerificationResult,
)
from scripts.probe_harness_sqlite_kill import (
    NOW,
    PROJECTION_NAME,
    append_request,
    projection,
)

VERIFIED_AT = NOW + timedelta(minutes=1)
Tamper = Callable[[sqlite3.Connection], None]


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


async def seed_journal(path: Path) -> None:
    journal = await SQLiteEventJournal.open(
        path,
        clock=lambda: NOW,
        projections=(projection(),),
    )
    await journal.append(append_request())
    await journal.close()


def tamper(path: Path, operation: Tamper) -> None:
    connection = sqlite3.connect(path)
    try:
        operation(connection)
        connection.commit()
    finally:
        connection.close()


def corrupt_event_tail(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER harness_events_no_update")
    connection.execute(
        """
        UPDATE harness_events
        SET event_sha256 = ?
        WHERE journal_sequence = (SELECT MAX(journal_sequence) FROM harness_events)
        """,
        ("0" * 64,),
    )


def corrupt_receipt(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT result_json FROM harness_idempotency LIMIT 1"
    ).fetchone()
    assert row is not None
    stored_result = json.loads(row[0])
    stored_result["receipt_sha256"] = "0" * 64
    connection.execute("DROP TRIGGER harness_idempotency_no_update")
    connection.execute(
        "UPDATE harness_idempotency SET result_json = ?",
        (json.dumps(stored_result, separators=(",", ":"), sort_keys=True),),
    )


def corrupt_event_receipt_link(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER harness_events_no_update")
    connection.execute(
        "UPDATE harness_events SET request_sha256 = ?",
        ("7" * 64,),
    )


def corrupt_projection(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER harness_projection_transition")
    connection.execute(
        "UPDATE harness_projection_checkpoints SET state_sha256 = ?",
        ("0" * 64,),
    )


def corrupt_sequence(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER harness_aggregates_monotonic")
    connection.execute(
        "UPDATE harness_aggregates SET current_sequence = current_sequence + 1"
    )


def corrupt_position(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER harness_journal_positions_monotonic")
    connection.execute(
        """
        UPDATE harness_journal_positions
        SET current_sequence = current_sequence + 1
        """
    )


def delete_health(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM harness_journal_health")


def second_request() -> AppendRequest:
    first_request = append_request()
    second_event = first_request.events[0].model_copy(
        update={
            "event_id": "evt_" + "2" * 32,
            "aggregate_sequence": 2,
            "occurred_at": NOW + timedelta(seconds=1),
        }
    )
    return first_request.model_copy(
        update={
            "expected_sequence": 1,
            "idempotency_key": "sqlite-integrity-second-command",
            "request_sha256": "8" * 64,
            "events": (second_event,),
        }
    )


def test_verification_is_bounded_then_persists_complete_health(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await seed_journal(path)
        verifier = await SQLiteJournalIntegrityVerifier.open(
            path,
            clock=lambda: VERIFIED_AT,
        )

        bounded = await verifier.verify(maximum_records=1)
        initial_health = await verifier.health()
        complete = await verifier.verify()
        health = await verifier.health()
        await verifier.close()

        assert not bounded.complete
        assert bounded.events_checked == 1
        assert bounded.receipts_checked == 0
        assert bounded.verified_at is None
        assert initial_health.verified_event_count == 0
        assert complete.complete
        assert complete.events_checked == 1
        assert complete.receipts_checked == 1
        assert complete.projections_checked == 1
        assert complete.verified_at == VERIFIED_AT
        assert health.status is JournalHealthStatus.HEALTHY
        assert health.failure_code is None
        assert health.verified_event_count == 1
        assert health.verified_at == VERIFIED_AT

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    (
        (corrupt_event_tail, JournalCorruptionCode.EVENT_CORRUPT),
        (corrupt_receipt, JournalCorruptionCode.RECEIPT_CORRUPT),
        (
            corrupt_event_receipt_link,
            JournalCorruptionCode.RECEIPT_CORRUPT,
        ),
        (corrupt_projection, JournalCorruptionCode.PROJECTION_CORRUPT),
        (corrupt_sequence, JournalCorruptionCode.SEQUENCE_CORRUPT),
        (corrupt_position, JournalCorruptionCode.SEQUENCE_CORRUPT),
    ),
)
def test_logical_corruption_persists_fail_closed_operator_state(
    tmp_path: Path,
    corruption: Tamper,
    expected_code: JournalCorruptionCode,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await seed_journal(path)
        tamper(path, corruption)
        verifier = await SQLiteJournalIntegrityVerifier.open(
            path,
            clock=lambda: VERIFIED_AT,
        )

        with pytest.raises(JournalCorruptionError) as failure:
            await verifier.verify()
        health = await verifier.health()
        with pytest.raises(JournalCorruptionError) as repeated:
            await verifier.verify()
        await verifier.close()

        assert failure.value.code is expected_code
        assert repeated.value.code is JournalCorruptionCode.NEEDS_OPERATOR
        assert health.status is JournalHealthStatus.NEEDS_OPERATOR
        assert health.failure_code == expected_code.value
        assert health.verified_at == VERIFIED_AT

        if expected_code is JournalCorruptionCode.PROJECTION_CORRUPT:
            connection = sqlite3.connect(path)
            try:
                projection_health = connection.execute(
                    """
                    SELECT projection_status, failure_code
                    FROM harness_projection_checkpoints
                    WHERE projection_name = ?
                    """,
                    (PROJECTION_NAME,),
                ).fetchone()
            finally:
                connection.close()
            assert projection_health == (
                "needs_operator",
                "checkpoint_corrupt",
            )

    asyncio.run(scenario())


def test_operator_state_blocks_future_event_inserts(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await seed_journal(path)
        tamper(path, corrupt_event_tail)
        verifier = await SQLiteJournalIntegrityVerifier.open(path)
        with pytest.raises(JournalCorruptionError):
            await verifier.verify()
        await verifier.close()

        journal = await SQLiteEventJournal.open(path)
        with pytest.raises(
            JournalStorageError,
            match="journal storage operation failed",
        ):
            await journal.append(second_request())
        await journal.close()

    asyncio.run(scenario())


def test_missing_health_row_blocks_future_event_inserts(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        await seed_journal(path)
        journal = await SQLiteEventJournal.open(path)
        tamper(path, delete_health)

        with pytest.raises(
            JournalStorageError,
            match="journal storage operation failed",
        ):
            await journal.append(second_request())
        await journal.close()

    asyncio.run(scenario())


def test_health_contracts_reject_inconsistent_states() -> None:
    with pytest.raises(ValidationError):
        JournalHealthRecord(
            generation=1,
            status=JournalHealthStatus.HEALTHY,
            failure_code="event_corrupt",
            verified_event_count=0,
            verified_at=VERIFIED_AT,
        )
    with pytest.raises(ValidationError):
        JournalVerificationResult(
            complete=True,
            events_checked=0,
            receipts_checked=0,
            projections_checked=0,
            verified_at=None,
        )
