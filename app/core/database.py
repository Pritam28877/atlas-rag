from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import DatabaseSettings


def normalize_database_url(database_url: str) -> str:
    """Select Psycopg explicitly while preserving URL components."""
    if database_url.startswith("postgresql+psycopg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    raise ValueError("database URL must use postgresql or postgresql+psycopg")


class Database:
    """Own the bounded database engine and transaction-scoped sessions."""

    def __init__(self, settings: DatabaseSettings) -> None:
        if settings.url is None:
            raise ValueError("database.url is required to initialize the database")

        database_url = normalize_database_url(settings.url.get_secret_value())
        max_overflow = settings.pool_max_size - settings.pool_min_size
        self._engine = create_async_engine(
            database_url,
            pool_size=settings.pool_min_size,
            max_overflow=max_overflow,
            pool_timeout=settings.pool_timeout_seconds,
            pool_pre_ping=True,
            connect_args={"connect_timeout": settings.connect_timeout_seconds},
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """Expose the engine for migration and application lifecycle integration."""
        return self._engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield one session with automatic commit or rollback and guaranteed close."""
        async with self._session_factory() as session:
            async with session.begin():
                yield session

    async def check_connection(self) -> None:
        """Run a minimal connectivity query without retaining a connection."""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        """Close the pool and all currently checked-in connections."""
        await self._engine.dispose()
