from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.harness.journal import (
    AppendRequest,
    AppendStatus,
    JournalDurability,
    JournalStorageError,
)
from app.services.harness.journal.postgres import PostgresEventJournal
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    TraceLink,
)

NOW = datetime(2026, 7, 26, 20, 0, tzinfo=UTC)


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def event(sequence: int) -> EventRecord:
    text_value = f"event-{sequence}"
    encoded_value = text_value.encode()
    return EventRecord(
        event_id=identifier("evt", sequence),
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
            text=text_value,
            size_bytes=len(encoded_value),
            content_sha256=hashlib.sha256(encoded_value).hexdigest(),
        ),
    )


def append_request(*events: EventRecord) -> AppendRequest:
    return AppendRequest(
        workspace_id=identifier("wsp"),
        aggregate_id=identifier("trn"),
        expected_sequence=0,
        idempotency_key="postgres-command-0001",
        request_sha256="a" * 64,
        durability=JournalDurability.SYNCHRONOUS,
        events=events,
    )


class MappingResult:
    def __init__(
        self,
        *,
        one: Mapping[str, object] | None = None,
        rows: tuple[Mapping[str, object], ...] = (),
    ) -> None:
        self._one = one
        self._rows = rows

    def mappings(self) -> MappingResult:
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        return self._one

    def all(self) -> tuple[Mapping[str, object], ...]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class RecordingSession:
    def __init__(
        self,
        *,
        scalar_values: list[object],
        execute_results: list[MappingResult],
        execute_error: SQLAlchemyError | None = None,
    ) -> None:
        self.scalar_values = scalar_values
        self.execute_results = execute_results
        self.execute_error = execute_error
        self.statements: list[tuple[str, Mapping[str, object]]] = []

    async def execute(
        self,
        statement,
        parameters: Mapping[str, object] | None = None,
    ) -> MappingResult:
        self.statements.append((str(statement), parameters or {}))
        if self.execute_error is not None:
            raise self.execute_error
        if self.execute_results:
            return self.execute_results.pop(0)
        return MappingResult()

    async def scalar(
        self,
        statement,
        parameters: Mapping[str, object] | None = None,
    ) -> object:
        self.statements.append((str(statement), parameters or {}))
        return self.scalar_values.pop(0)


class TransactionQueue:
    def __init__(self, *sessions: RecordingSession) -> None:
        self._sessions = list(sessions)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        session = self._sessions.pop(0)
        yield cast(AsyncSession, session)


def append_session() -> RecordingSession:
    return RecordingSession(
        scalar_values=[0, 0, NOW, 2, 2],
        execute_results=[
            MappingResult(),
            MappingResult(),
            MappingResult(),
            MappingResult(),
            MappingResult(
                rows=(
                    {"aggregate_sequence": 1, "journal_sequence": 1},
                    {"aggregate_sequence": 2, "journal_sequence": 2},
                )
            ),
            MappingResult(),
        ],
    )


def test_append_is_one_bounded_batch_with_workspace_cursor() -> None:
    async def scenario() -> None:
        session = append_session()
        transactions = TransactionQueue(session)
        journal = PostgresEventJournal(transactions.transaction)

        result = await journal.append(append_request(event(1), event(2)))

        assert result.journal_sequences == (1, 2)
        assert result.status is AppendStatus.APPENDED
        statement_text = "\n".join(statement for statement, _ in session.statements)
        assert "SET LOCAL synchronous_commit TO ON" in statement_text
        assert statement_text.count("INSERT INTO harness_journal_events") == 1
        assert "UPDATE harness_journal_positions" in statement_text

    asyncio.run(scenario())


def test_idempotent_replay_preserves_the_original_receipt() -> None:
    async def scenario() -> None:
        first_session = append_session()
        first_transactions = TransactionQueue(first_session)
        first_journal = PostgresEventJournal(first_transactions.transaction)
        request = append_request(event(1), event(2))
        original = await first_journal.append(request)

        replay_session = RecordingSession(
            scalar_values=[2],
            execute_results=[
                MappingResult(),
                MappingResult(),
                MappingResult(
                    one={
                        "request_sha256": request.request_sha256,
                        "result_json": original.model_dump_json(),
                    }
                ),
            ],
        )
        replay_transactions = TransactionQueue(replay_session)
        replay_journal = PostgresEventJournal(replay_transactions.transaction)
        replay = await replay_journal.append(request)

        assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
        assert replay.receipt_sha256 == original.receipt_sha256
        assert replay.committed_at == original.committed_at
        assert all(
            "harness_journal_positions" not in statement
            for statement, _ in replay_session.statements
        )

    asyncio.run(scenario())


def test_sqlalchemy_failures_are_sanitized() -> None:
    async def scenario() -> None:
        session = RecordingSession(
            scalar_values=[],
            execute_results=[],
            execute_error=SQLAlchemyError("private database detail"),
        )
        transactions = TransactionQueue(session)
        journal = PostgresEventJournal(transactions.transaction)

        with pytest.raises(
            JournalStorageError,
            match="journal storage operation failed",
        ) as failure:
            await journal.append(append_request(event(1)))
        assert "private database detail" not in str(failure.value)

    asyncio.run(scenario())
