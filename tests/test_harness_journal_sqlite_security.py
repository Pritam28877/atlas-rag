import asyncio
import os
from pathlib import Path

import pytest

from app.services.harness.journal import (
    JournalReadRequest,
    JournalStorageError,
    SQLiteEventJournal,
)


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def test_location_and_closed_state_fail_safely(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        os.chmod(tmp_path, 0o755)
        with pytest.raises(JournalStorageError, match="owner-only"):
            await SQLiteEventJournal.open(path)
        assert not path.exists()

        os.chmod(tmp_path, 0o700)
        path.write_text("not private", encoding="utf-8")
        os.chmod(path, 0o644)
        with pytest.raises(JournalStorageError, match="not private"):
            await SQLiteEventJournal.open(path)

        path.unlink()
        journal = await SQLiteEventJournal.open(path)
        await journal.close()
        with pytest.raises(JournalStorageError, match="closed"):
            await journal.read_aggregate(
                JournalReadRequest(
                    workspace_id=identifier("wsp"),
                    aggregate_id=identifier("trn"),
                    after_sequence=0,
                )
            )

    asyncio.run(scenario())
