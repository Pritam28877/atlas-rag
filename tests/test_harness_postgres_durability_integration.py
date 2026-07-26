import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import Database
from app.services.harness.journal import (
    AppendStatus,
    GlobalJournalReadRequest,
    JournalStorageError,
)
from app.services.harness.journal.faults import JournalFaultPoint
from app.services.harness.journal.postgres import PostgresEventJournal
from app.services.harness.journal.postgres_projection_store import (
    PostgresProjectionStore,
)
from tests.harness_postgres_integration_support import (
    PROJECTION_NAME,
    append_request,
    count_projection,
    prepare_database,
)

PRECOMMIT_FAULT_POINTS = (
    JournalFaultPoint.AFTER_EVENTS_INSERTED,
    JournalFaultPoint.AFTER_AGGREGATE_UPDATED,
    JournalFaultPoint.AFTER_PROJECTIONS_APPLIED,
    JournalFaultPoint.BEFORE_COMMIT,
)


@pytest.mark.database_integration
@pytest.mark.parametrize("target", PRECOMMIT_FAULT_POINTS)
def test_precommit_fault_rolls_back_events_and_projection(
    monkeypatch: pytest.MonkeyPatch,
    target: JournalFaultPoint,
) -> None:
    prepare_database(monkeypatch)

    class InjectedFault(RuntimeError):
        pass

    async def scenario() -> None:
        database = Database(get_settings().database)
        request = append_request()

        def fail(fault_point: JournalFaultPoint) -> None:
            if fault_point is target:
                raise InjectedFault

        journal = PostgresEventJournal(
            database.transaction,
            projections=(count_projection(),),
            fault_injector=fail,
        )
        projection_store = PostgresProjectionStore(database.transaction)
        try:
            with pytest.raises(InjectedFault):
                await journal.append(request)
            page = await journal.read_global(
                GlobalJournalReadRequest(
                    workspace_id=request.workspace_id,
                    after_journal_sequence=0,
                )
            )
            stored_projection = await projection_store.load(
                request.workspace_id,
                PROJECTION_NAME,
            )
        finally:
            await database.close()

        assert page.events == ()
        assert stored_projection is None

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()


@pytest.mark.database_integration
def test_terminated_backend_rolls_back_uncommitted_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_database(monkeypatch)

    async def scenario() -> None:
        journal_database = Database(get_settings().database)
        killer_database = Database(get_settings().database)
        request = append_request()
        active_session: AsyncSession | None = None

        @asynccontextmanager
        async def tracked_transaction() -> AsyncIterator[AsyncSession]:
            nonlocal active_session
            async with journal_database.transaction() as session:
                active_session = session
                try:
                    yield session
                finally:
                    active_session = None

        async def terminate_backend(fault_point: JournalFaultPoint) -> None:
            if fault_point is not JournalFaultPoint.BEFORE_COMMIT:
                return
            if active_session is None:
                raise RuntimeError("journal session is unavailable")
            backend_pid = await active_session.scalar(
                text("SELECT pg_backend_pid()")
            )
            async with killer_database.transaction() as killer:
                terminated = await killer.scalar(
                    text("SELECT pg_terminate_backend(:backend_pid)"),
                    {"backend_pid": backend_pid},
                )
            if terminated is not True:
                raise RuntimeError("journal backend was not terminated")

        journal = PostgresEventJournal(
            tracked_transaction,
            projections=(count_projection(),),
            fault_injector=terminate_backend,
        )
        try:
            with pytest.raises(
                JournalStorageError,
                match="journal storage operation failed",
            ):
                await journal.append(request)
        finally:
            await journal_database.close()
            await killer_database.close()

        verification_database = Database(get_settings().database)
        verification_journal = PostgresEventJournal(
            verification_database.transaction
        )
        projection_store = PostgresProjectionStore(
            verification_database.transaction
        )
        try:
            page = await verification_journal.read_global(
                GlobalJournalReadRequest(
                    workspace_id=request.workspace_id,
                    after_journal_sequence=0,
                )
            )
            stored_projection = await projection_store.load(
                request.workspace_id,
                PROJECTION_NAME,
            )
        finally:
            await verification_database.close()

        assert page.events == ()
        assert stored_projection is None

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()


@pytest.mark.database_integration
def test_postcommit_response_loss_replays_durable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_database(monkeypatch)

    class ResponseLost(RuntimeError):
        pass

    async def scenario() -> None:
        database = Database(get_settings().database)
        request = append_request()
        response_lost = False

        def lose_response(fault_point: JournalFaultPoint) -> None:
            nonlocal response_lost
            if (
                fault_point is JournalFaultPoint.AFTER_COMMIT
                and not response_lost
            ):
                response_lost = True
                raise ResponseLost

        journal = PostgresEventJournal(
            database.transaction,
            projections=(count_projection(),),
            fault_injector=lose_response,
        )
        projection_store = PostgresProjectionStore(database.transaction)
        try:
            with pytest.raises(ResponseLost):
                await journal.append(request)
            replay = await journal.append(request)
            page = await journal.read_global(
                GlobalJournalReadRequest(
                    workspace_id=request.workspace_id,
                    after_journal_sequence=0,
                )
            )
            stored_projection = await projection_store.load(
                request.workspace_id,
                PROJECTION_NAME,
            )
        finally:
            await database.close()

        assert replay.status is AppendStatus.IDEMPOTENT_REPLAY
        assert len(page.events) == 1
        assert stored_projection is not None
        assert stored_projection.checkpoint.state_json == '{"count":1}'

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()


@pytest.mark.database_integration
def test_missing_health_row_blocks_postgres_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_database(monkeypatch)

    async def scenario() -> None:
        database = Database(get_settings().database)
        journal = PostgresEventJournal(database.transaction)
        request = append_request()
        try:
            async with database.transaction() as session:
                await session.execute(text("DELETE FROM harness_journal_health"))
            with pytest.raises(
                JournalStorageError,
                match="journal storage operation failed",
            ):
                await journal.append(request)
        finally:
            async with database.transaction() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO harness_journal_health (singleton)
                        VALUES (TRUE)
                        ON CONFLICT (singleton) DO NOTHING
                        """
                    )
                )
            await database.close()

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()
