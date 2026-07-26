import asyncio

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.harness.journal import (
    AppendStatus,
    JournalStorageError,
)
from app.services.harness.journal.faults import JournalFaultPoint
from app.services.harness.journal.postgres import PostgresEventJournal
from app.services.harness.journal.receipts import build_append_result
from tests.harness_postgres_unit_support import (
    NOW,
    MappingResult,
    RecordingSession,
    TransactionQueue,
    append_request,
    append_session,
    event,
)


def test_fault_boundaries_wrap_the_transaction_commit() -> None:
    async def scenario() -> None:
        session = append_session()
        transactions = TransactionQueue(session)
        observed: list[JournalFaultPoint] = []

        async def record(fault_point: JournalFaultPoint) -> None:
            observed.append(fault_point)

        journal = PostgresEventJournal(
            transactions.transaction,
            fault_injector=record,
        )
        await journal.append(append_request(event(1), event(2)))

        assert observed == list(JournalFaultPoint)
        assert transactions.committed == 1
        assert transactions.rolled_back == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    (
        JournalFaultPoint.AFTER_EVENTS_INSERTED,
        JournalFaultPoint.AFTER_AGGREGATE_UPDATED,
        JournalFaultPoint.AFTER_PROJECTIONS_APPLIED,
        JournalFaultPoint.BEFORE_COMMIT,
    ),
)
def test_precommit_faults_rollback_the_transaction(
    target: JournalFaultPoint,
) -> None:
    class InjectedFault(RuntimeError):
        pass

    async def scenario() -> None:
        session = append_session()
        transactions = TransactionQueue(session)

        def fail(fault_point: JournalFaultPoint) -> None:
            if fault_point is target:
                raise InjectedFault

        journal = PostgresEventJournal(
            transactions.transaction,
            fault_injector=fail,
        )
        with pytest.raises(InjectedFault):
            await journal.append(append_request(event(1), event(2)))

        assert transactions.committed == 0
        assert transactions.rolled_back == 1

    asyncio.run(scenario())


def test_postcommit_response_loss_is_idempotently_recoverable() -> None:
    class ResponseLost(RuntimeError):
        pass

    async def scenario() -> None:
        request = append_request(event(1), event(2))
        committed_result = build_append_result(request, NOW, (1, 2))
        first_session = append_session()
        replay_session = RecordingSession(
            scalar_values=[2],
            execute_results=[
                MappingResult(),
                MappingResult(),
                MappingResult(
                    one={
                        "request_sha256": request.request_sha256,
                        "result_json": committed_result.model_dump_json(),
                    }
                ),
            ],
        )
        transactions = TransactionQueue(first_session, replay_session)
        response_available = False

        def lose_first_response(fault_point: JournalFaultPoint) -> None:
            nonlocal response_available
            if (
                fault_point is JournalFaultPoint.AFTER_COMMIT
                and not response_available
            ):
                response_available = True
                raise ResponseLost

        journal = PostgresEventJournal(
            transactions.transaction,
            fault_injector=lose_first_response,
        )
        with pytest.raises(ResponseLost):
            await journal.append(request)
        replay = await journal.append(request)

        assert transactions.committed == 2
        assert transactions.rolled_back == 0
        assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
        assert replay.receipt_sha256 == committed_result.receipt_sha256

    asyncio.run(scenario())


def test_fault_injector_database_errors_are_sanitized() -> None:
    async def scenario() -> None:
        transactions = TransactionQueue(append_session())

        def fail(fault_point: JournalFaultPoint) -> None:
            if fault_point is JournalFaultPoint.BEFORE_COMMIT:
                raise SQLAlchemyError("private fault database detail")

        journal = PostgresEventJournal(
            transactions.transaction,
            fault_injector=fail,
        )
        with pytest.raises(
            JournalStorageError,
            match="journal storage operation failed",
        ) as failure:
            await journal.append(append_request(event(1), event(2)))
        assert "private fault database detail" not in str(failure.value)
        assert transactions.rolled_back == 1

    asyncio.run(scenario())
