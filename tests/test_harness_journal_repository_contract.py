import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.core.database import Database
from app.services.harness.journal import (
    AppendRequest,
    AppendResult,
    AppendStatus,
    EventJournal,
    GlobalJournalReadRequest,
    JournalConflictCode,
    JournalConflictError,
    JournalDurability,
    JournalReadRequest,
    SQLiteEventJournal,
)
from app.services.harness.journal.postgres import PostgresEventJournal
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    TraceLink,
)
from tests.harness_postgres_integration_support import prepare_database

Backend = Literal["sqlite", "postgres"]


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def event(
    aggregate_id: str,
    sequence: int,
    *,
    event_id: str,
    occurred_at: datetime,
) -> EventRecord:
    text_value = f"repository-contract-event-{sequence}"
    encoded_value = text_value.encode()
    return EventRecord(
        event_id=event_id,
        event_type="Turn.Accepted",
        schema_version="1.2",
        aggregate_id=aggregate_id,
        aggregate_sequence=sequence,
        actor_kind=EventActorKind.SYSTEM,
        actor_principal_id=identifier("prn"),
        occurred_at=occurred_at + timedelta(seconds=sequence),
        trace=TraceLink(
            request_id=identifier("req"),
            correlation_id=identifier("evt"),
        ),
        payload=InlinePayload(
            text=text_value,
            size_bytes=len(encoded_value),
            content_sha256=hashlib.sha256(encoded_value).hexdigest(),
        ),
    )


def request(
    *events: EventRecord,
    workspace_id: str,
    aggregate_id: str,
    expected_sequence: int,
    key_suffix: str,
    digest_character: str,
) -> AppendRequest:
    return AppendRequest(
        workspace_id=workspace_id,
        aggregate_id=aggregate_id,
        expected_sequence=expected_sequence,
        idempotency_key=f"repository-contract-{key_suffix}",
        request_sha256=digest_character * 64,
        durability=JournalDurability.SYNCHRONOUS,
        events=events,
    )


async def assert_repository_contract(journal: EventJournal) -> None:
    workspace_id = identifier("wsp")
    other_workspace_id = identifier("wsp")
    aggregate_id = identifier("trn")
    base_time = datetime.now(UTC)
    first_event = event(
        aggregate_id,
        1,
        event_id=identifier("evt"),
        occurred_at=base_time,
    )
    second_event = event(
        aggregate_id,
        2,
        event_id=identifier("evt"),
        occurred_at=base_time,
    )
    first_request = request(
        first_event,
        second_event,
        workspace_id=workspace_id,
        aggregate_id=aggregate_id,
        expected_sequence=0,
        key_suffix=uuid4().hex,
        digest_character="a",
    )

    appended = await journal.append(first_request)
    replay = await journal.append(first_request)
    aggregate_page = await journal.read_aggregate(
        JournalReadRequest(
            workspace_id=workspace_id,
            aggregate_id=aggregate_id,
            after_sequence=0,
            limit=1,
        )
    )
    global_page = await journal.read_global(
        GlobalJournalReadRequest(
            workspace_id=workspace_id,
            after_journal_sequence=0,
            limit=1,
        )
    )
    isolated = await journal.append(
        request(
            first_event,
            workspace_id=other_workspace_id,
            aggregate_id=aggregate_id,
            expected_sequence=0,
            key_suffix=uuid4().hex,
            digest_character="b",
        )
    )
    with pytest.raises(JournalConflictError) as idempotency_mismatch:
        await journal.append(
            first_request.model_copy(
                update={"request_sha256": "f" * 64}
            )
        )

    competing_requests = (
        request(
            event(
                aggregate_id,
                3,
                event_id=identifier("evt"),
                occurred_at=base_time,
            ),
            workspace_id=workspace_id,
            aggregate_id=aggregate_id,
            expected_sequence=2,
            key_suffix=uuid4().hex,
            digest_character="c",
        ),
        request(
            event(
                aggregate_id,
                3,
                event_id=identifier("evt"),
                occurred_at=base_time,
            ),
            workspace_id=workspace_id,
            aggregate_id=aggregate_id,
            expected_sequence=2,
            key_suffix=uuid4().hex,
            digest_character="d",
        ),
    )
    outcomes = await asyncio.gather(
        *(journal.append(candidate) for candidate in competing_requests),
        return_exceptions=True,
    )
    final_aggregate = await journal.read_aggregate(
        JournalReadRequest(
            workspace_id=workspace_id,
            aggregate_id=aggregate_id,
            after_sequence=0,
        )
    )
    final_global = await journal.read_global(
        GlobalJournalReadRequest(
            workspace_id=workspace_id,
            after_journal_sequence=0,
        )
    )

    appended_outcomes = [
        outcome for outcome in outcomes if isinstance(outcome, AppendResult)
    ]
    conflict_outcomes = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, JournalConflictError)
    ]
    assert appended.status is AppendStatus.APPENDED
    assert appended.journal_sequences == (1, 2)
    assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
    assert replay.receipt_sha256 == appended.receipt_sha256
    assert aggregate_page.has_more
    assert tuple(item.aggregate_sequence for item in aggregate_page.events) == (1,)
    assert global_page.has_more
    assert tuple(item.journal_sequence for item in global_page.events) == (1,)
    assert isolated.journal_sequences == (1,)
    assert (
        idempotency_mismatch.value.code
        is JournalConflictCode.IDEMPOTENCY_MISMATCH
    )
    assert idempotency_mismatch.value.current_sequence == 2
    assert len(appended_outcomes) == 1
    assert appended_outcomes[0].journal_sequences == (3,)
    assert len(conflict_outcomes) == 1
    assert conflict_outcomes[0].code is JournalConflictCode.EXPECTED_SEQUENCE
    assert conflict_outcomes[0].current_sequence == 3
    assert tuple(
        item.aggregate_sequence for item in final_aggregate.events
    ) == (1, 2, 3)
    assert tuple(
        item.journal_sequence for item in final_global.events
    ) == (1, 2, 3)


@pytest.mark.database_integration
@pytest.mark.parametrize("backend", ("sqlite", "postgres"))
def test_event_journal_repository_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: Backend,
) -> None:
    if backend == "postgres":
        prepare_database(monkeypatch)

    async def scenario() -> None:
        if backend == "sqlite":
            os.chmod(tmp_path, 0o700)
            sqlite_journal = await SQLiteEventJournal.open(
                tmp_path / "repository-contract.sqlite3"
            )
            try:
                await assert_repository_contract(sqlite_journal)
            finally:
                await sqlite_journal.close()
            return

        database = Database(get_settings().database)
        postgres_journal = PostgresEventJournal(database.transaction)
        try:
            await assert_repository_contract(postgres_journal)
        finally:
            await database.close()

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()
