import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.database import Database
from app.services.harness.journal import (
    AppendRequest,
    AppendStatus,
    GlobalJournalReadRequest,
    JournalConflictCode,
    JournalConflictError,
    JournalDurability,
    JournalReadRequest,
)
from app.services.harness.journal.postgres import PostgresEventJournal
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    TraceLink,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def event(
    sequence: int,
    *,
    aggregate_id: str,
    event_id: str,
    occurred_at: datetime,
) -> EventRecord:
    text_value = f"integration-event-{sequence}"
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
    request_sha256: str,
) -> AppendRequest:
    return AppendRequest(
        workspace_id=workspace_id,
        aggregate_id=aggregate_id,
        expected_sequence=expected_sequence,
        idempotency_key=f"postgres-integration-{key_suffix}",
        request_sha256=request_sha256,
        durability=JournalDurability.SYNCHRONOUS,
        events=events,
    )


@pytest.mark.database_integration
def test_postgres_journal_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    parsed_url = make_url(database_url)
    if parsed_url.database is None or not parsed_url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")

    monkeypatch.setenv("DATABASE__URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")

    async def scenario() -> None:
        database = Database(get_settings().database)
        journal = PostgresEventJournal(database.transaction)
        workspace_id = identifier("wsp")
        other_workspace_id = identifier("wsp")
        aggregate_id = identifier("trn")
        first_event_id = identifier("evt")
        base_time = datetime.now(UTC)
        first_request = request(
            event(
                1,
                aggregate_id=aggregate_id,
                event_id=first_event_id,
                occurred_at=base_time,
            ),
            event(
                2,
                aggregate_id=aggregate_id,
                event_id=identifier("evt"),
                occurred_at=base_time,
            ),
            workspace_id=workspace_id,
            aggregate_id=aggregate_id,
            expected_sequence=0,
            key_suffix=uuid4().hex,
            request_sha256="a" * 64,
        )
        try:
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
                    event(
                        1,
                        aggregate_id=aggregate_id,
                        event_id=first_event_id,
                        occurred_at=base_time,
                    ),
                    workspace_id=other_workspace_id,
                    aggregate_id=aggregate_id,
                    expected_sequence=0,
                    key_suffix=uuid4().hex,
                    request_sha256="b" * 64,
                )
            )

            competing_requests = (
                request(
                    event(
                        3,
                        aggregate_id=aggregate_id,
                        event_id=identifier("evt"),
                        occurred_at=base_time,
                    ),
                    workspace_id=workspace_id,
                    aggregate_id=aggregate_id,
                    expected_sequence=2,
                    key_suffix=uuid4().hex,
                    request_sha256="c" * 64,
                ),
                request(
                    event(
                        3,
                        aggregate_id=aggregate_id,
                        event_id=identifier("evt"),
                        occurred_at=base_time,
                    ),
                    workspace_id=workspace_id,
                    aggregate_id=aggregate_id,
                    expected_sequence=2,
                    key_suffix=uuid4().hex,
                    request_sha256="d" * 64,
                ),
            )
            outcomes = await asyncio.gather(
                *(journal.append(candidate) for candidate in competing_requests),
                return_exceptions=True,
            )
        finally:
            await database.close()

        assert appended.status is AppendStatus.APPENDED
        assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
        assert replay.receipt_sha256 == appended.receipt_sha256
        assert tuple(item.aggregate_sequence for item in aggregate_page.events) == (1,)
        assert aggregate_page.has_more
        assert tuple(item.journal_sequence for item in global_page.events) == (1,)
        assert global_page.has_more
        assert isolated.journal_sequences == (1,)
        conflicts = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, JournalConflictError)
        ]
        assert len(conflicts) == 1
        assert conflicts[0].code is JournalConflictCode.EXPECTED_SEQUENCE

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()
