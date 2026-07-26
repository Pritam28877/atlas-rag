"""Bounded thread, connection, and private-path ownership for SQLite."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from app.services.harness.journal.errors import (
    JournalBusyError,
    JournalStorageError,
)
from app.services.harness.journal.sqlite_migrations import (
    SQLITE_MIGRATE_V1_TO_V2,
    SQLITE_MIGRATE_V2_TO_V3,
)
from app.services.harness.journal.sqlite_schema import (
    SQLITE_SCHEMA,
    SQLITE_SCHEMA_VERSION,
)

ResultType = TypeVar("ResultType")


class SQLiteConnectionOwner:
    """Owns one SQLite connection on one bounded worker thread."""

    def __init__(
        self,
        database_path: Path,
        *,
        busy_timeout_ms: int,
        maximum_pending_operations: int,
    ) -> None:
        if not database_path.is_absolute():
            raise ValueError("SQLite journal path must be absolute")
        if not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("SQLite busy timeout must be between 1 and 60000 ms")
        if not 1 <= maximum_pending_operations <= 1024:
            raise ValueError(
                "maximum pending journal operations must be between 1 and 1024"
            )
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms
        self._maximum_pending_operations = maximum_pending_operations
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="atlas-journal-sqlite",
        )
        self._connection: sqlite3.Connection | None = None
        self._pending_operations = 0
        self._closed = False

    async def initialize(self) -> None:
        try:
            await self._run(self._initialize)
        except BaseException:
            await self.close()
            raise

    async def execute(
        self,
        operation: Callable[[sqlite3.Connection], ResultType],
    ) -> ResultType:
        return await self._run(
            lambda: operation(self._require_connection())
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            await self._submit(self._close_connection)
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def _run(self, operation: Callable[[], ResultType]) -> ResultType:
        if self._closed:
            raise JournalStorageError("journal storage is closed")
        if self._pending_operations >= self._maximum_pending_operations:
            raise JournalBusyError("journal operation capacity is exhausted")
        self._pending_operations += 1
        operation_future = self._submit(operation)
        try:
            return await asyncio.shield(operation_future)
        except (JournalBusyError, JournalStorageError):
            raise
        except sqlite3.Error as error:
            raise JournalStorageError("journal storage operation failed") from error
        finally:
            if operation_future.done():
                self._pending_operations -= 1
            else:
                operation_future.add_done_callback(
                    self._release_pending_operation
                )

    def _submit(
        self,
        operation: Callable[[], ResultType],
    ) -> asyncio.Future[ResultType]:
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._executor, operation)

    def _release_pending_operation(
        self,
        operation_future: asyncio.Future[ResultType],
    ) -> None:
        self._pending_operations -= 1
        if not operation_future.cancelled():
            operation_future.exception()

    def _initialize(self) -> None:
        self._validate_location()
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            schema_version = self._schema_version(connection)
            if schema_version is None:
                connection.executescript(SQLITE_SCHEMA)
            elif schema_version["schema_version"] == 1:
                connection.executescript(SQLITE_MIGRATE_V1_TO_V2)
                connection.executescript(SQLITE_MIGRATE_V2_TO_V3)
                connection.executescript(SQLITE_SCHEMA)
            elif schema_version["schema_version"] == 2:
                connection.executescript(SQLITE_MIGRATE_V2_TO_V3)
                connection.executescript(SQLITE_SCHEMA)
            elif schema_version["schema_version"] == SQLITE_SCHEMA_VERSION:
                connection.executescript(SQLITE_SCHEMA)
            else:
                raise JournalStorageError("unsupported SQLite journal schema")
            connection.execute("PRAGMA foreign_keys = ON")
            migrated_version = self._schema_version(connection)
            if (
                migrated_version is None
                or migrated_version["schema_version"] != SQLITE_SCHEMA_VERSION
            ):
                raise JournalStorageError("unsupported SQLite journal schema")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise JournalStorageError(
                    "SQLite journal foreign keys are invalid"
                )
            os.chmod(self._database_path, 0o600)
            self._connection = connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _schema_version(connection: sqlite3.Connection) -> sqlite3.Row | None:
        schema_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'harness_journal_schema'
            """
        ).fetchone()
        if schema_table is None:
            return None
        schema_version = connection.execute(
            """
            SELECT schema_version
            FROM harness_journal_schema
            WHERE singleton = 1
            """
        ).fetchone()
        if schema_version is None:
            raise JournalStorageError("SQLite journal schema version is missing")
        if not isinstance(schema_version, sqlite3.Row):
            raise JournalStorageError("SQLite journal schema version is invalid")
        return schema_version

    def _validate_location(self) -> None:
        parent = self._database_path.parent
        if parent.resolve(strict=True) != parent:
            raise JournalStorageError("journal directory must be canonical")
        parent_status = parent.stat()
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or parent_status.st_uid != os.getuid()
            or stat.S_IMODE(parent_status.st_mode) & 0o077
        ):
            raise JournalStorageError("journal directory must be owner-only")
        if os.path.lexists(self._database_path):
            file_status = self._database_path.lstat()
            if (
                not stat.S_ISREG(file_status.st_mode)
                or file_status.st_uid != os.getuid()
                or stat.S_IMODE(file_status.st_mode) & 0o077
            ):
                raise JournalStorageError("journal file is not private")

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise JournalStorageError("journal storage is unavailable")
        return self._connection

    def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()
