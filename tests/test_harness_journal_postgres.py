from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.harness.journal import (
    AppendStatus,
    JournalStorageError,
)
from app.services.harness.journal.postgres import PostgresEventJournal
from tests.harness_postgres_unit_support import (
    MappingResult,
    RecordingSession,
    TransactionQueue,
    append_request,
    append_session,
    count_projection,
    event,
    missing_projection_history_session,
    projected_append_session,
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


def test_append_applies_registered_projection_in_the_same_session() -> None:
    async def scenario() -> None:
        session = projected_append_session()
        transactions = TransactionQueue(session)
        journal = PostgresEventJournal(
            transactions.transaction,
            projections=(count_projection(),),
        )

        await journal.append(append_request(event(1), event(2)))

        projection_writes = [
            (statement, parameters)
            for statement, parameters in session.statements
            if "INSERT INTO harness_projection_checkpoints" in statement
        ]
        assert len(projection_writes) == 1
        assert projection_writes[0][1]["last_journal_sequence"] == 2
        assert projection_writes[0][1]["state_json"] == '{"count":2}'
        assert "INSERT INTO harness_journal_idempotency" in (
            session.statements[-1][0]
        )

    asyncio.run(scenario())


def test_append_rejects_projection_with_missing_history() -> None:
    async def scenario() -> None:
        session = missing_projection_history_session()
        transactions = TransactionQueue(session)
        journal = PostgresEventJournal(
            transactions.transaction,
            projections=(count_projection(),),
        )

        with pytest.raises(
            JournalStorageError,
            match="requires a complete rebuild",
        ):
            await journal.append(append_request(event(1), event(2)))
        assert all(
            "INSERT INTO harness_journal_idempotency" not in statement
            for statement, _ in session.statements
        )

    asyncio.run(scenario())


def test_online_projection_registration_is_unique_and_bounded() -> None:
    transactions = TransactionQueue()
    duplicate = count_projection()
    with pytest.raises(ValueError, match="unique"):
        PostgresEventJournal(
            transactions.transaction,
            projections=(duplicate, duplicate),
        )
    with pytest.raises(ValueError, match="exceeds 32"):
        PostgresEventJournal(
            transactions.transaction,
            projections=tuple(count_projection() for _ in range(33)),
        )


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
