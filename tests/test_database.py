import asyncio
from types import TracebackType
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.core.config import DatabaseSettings
from app.core.database import Database, normalize_database_url


def test_normalize_database_url_selects_psycopg_driver() -> None:
    assert normalize_database_url("postgresql://db/rag") == (
        "postgresql+psycopg://db/rag"
    )
    assert normalize_database_url("postgresql+psycopg://db/rag") == (
        "postgresql+psycopg://db/rag"
    )


def test_normalize_database_url_rejects_other_drivers() -> None:
    with pytest.raises(ValueError, match="must use postgresql"):
        normalize_database_url("sqlite:///rag.db")


def test_database_uses_configured_bounded_pool() -> None:
    settings = DatabaseSettings(
        url=SecretStr("postgresql://user:password@localhost/rag"),
        pool_min_size=3,
        pool_max_size=8,
    )

    database = Database(settings)

    assert isinstance(database.engine.pool, AsyncAdaptedQueuePool)
    assert database.engine.pool.size() == 3
    assert database.engine.pool._max_overflow == 5


def test_database_close_disposes_engine() -> None:
    settings = DatabaseSettings(
        url=SecretStr("postgresql://user:password@localhost/rag")
    )
    database = Database(settings)
    with patch.object(AsyncEngine, "dispose", new_callable=AsyncMock) as dispose:
        asyncio.run(database.close())

    dispose.assert_awaited_once_with()


class RecordingTransaction:
    def __init__(self) -> None:
        self.entered = False
        self.exit_exception: type[BaseException] | None = None

    async def __aenter__(self) -> None:
        self.entered = True

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_exception = exception_type


class RecordingSession:
    def __init__(self) -> None:
        self.transaction = RecordingTransaction()
        self.closed = False

    async def __aenter__(self) -> "RecordingSession":
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    def begin(self) -> RecordingTransaction:
        return self.transaction


def create_database_with_recording_session() -> tuple[Database, RecordingSession]:
    settings = DatabaseSettings(
        url=SecretStr("postgresql://user:password@localhost/rag")
    )
    database = Database(settings)
    session = RecordingSession()
    setattr(database, "_session_factory", lambda: session)
    return database, session


def test_transaction_commits_and_closes_on_success() -> None:
    database, session = create_database_with_recording_session()

    async def use_transaction() -> None:
        async with database.transaction() as yielded_session:
            assert yielded_session is session

    asyncio.run(use_transaction())

    assert session.transaction.entered
    assert session.transaction.exit_exception is None
    assert session.closed


def test_transaction_rolls_back_and_closes_on_exception() -> None:
    database, session = create_database_with_recording_session()

    async def fail_transaction() -> None:
        async with database.transaction():
            raise RuntimeError("expected failure")

    with pytest.raises(RuntimeError, match="expected failure"):
        asyncio.run(fail_transaction())

    assert session.transaction.exit_exception is RuntimeError
    assert session.closed
