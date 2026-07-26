from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.postgres_projection_store import (
    PostgresProjectionStore,
)
from app.services.harness.journal.projection_contracts import ProjectionCheckpoint
from app.services.harness.journal.projection_store import (
    ProjectionExpectation,
    ProjectionHealth,
    ProjectionStoreConflict,
)

NOW = datetime(2026, 7, 26, 23, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "3" * 32
PROJECTION_NAME = "postgres_counter"


def checkpoint(sequence: int, count: int) -> ProjectionCheckpoint:
    state_json = f'{{"count":{count}}}'
    return ProjectionCheckpoint(
        workspace_id=WORKSPACE_ID,
        projection_name=PROJECTION_NAME,
        projection_version="1.0",
        last_journal_sequence=sequence,
        event_count=count,
        state_json=state_json,
        state_sha256=hashlib.sha256(state_json.encode()).hexdigest(),
    )


def stored_row(
    projection_checkpoint: ProjectionCheckpoint,
    *,
    generation: int,
    health: str = "healthy",
    failure_code: str | None = None,
    updated_at: object = NOW,
) -> Mapping[str, object]:
    return {
        "generation": generation,
        "workspace_id": projection_checkpoint.workspace_id,
        "projection_name": projection_checkpoint.projection_name,
        "projection_version": projection_checkpoint.projection_version,
        "last_journal_sequence": projection_checkpoint.last_journal_sequence,
        "event_count": projection_checkpoint.event_count,
        "state_json": projection_checkpoint.state_json,
        "state_sha256": projection_checkpoint.state_sha256,
        "projection_status": health,
        "failure_code": failure_code,
        "updated_at": updated_at,
    }


class MappingResult:
    def __init__(self, row: Mapping[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> MappingResult:
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        return self._row


class RecordingSession:
    def __init__(self, *rows: Mapping[str, object] | None) -> None:
        self._results = [MappingResult(row) for row in rows]
        self.statements: list[tuple[str, Mapping[str, object]]] = []

    async def execute(
        self,
        statement,
        parameters: Mapping[str, object] | None = None,
    ) -> MappingResult:
        self.statements.append((str(statement), parameters or {}))
        return self._results.pop(0)


class TransactionQueue:
    def __init__(self, *sessions: RecordingSession) -> None:
        self._sessions = list(sessions)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, self._sessions.pop(0))


def test_insert_advance_and_rebuild_use_explicit_cas() -> None:
    async def scenario() -> None:
        first_checkpoint = checkpoint(1, 1)
        second_checkpoint = checkpoint(2, 2)
        insert_session = RecordingSession(
            stored_row(first_checkpoint, generation=1)
        )
        advance_session = RecordingSession(
            stored_row(second_checkpoint, generation=1)
        )
        rebuild_session = RecordingSession(
            stored_row(second_checkpoint, generation=2)
        )
        transactions = TransactionQueue(
            insert_session,
            advance_session,
            rebuild_session,
        )
        store = PostgresProjectionStore(transactions.transaction)

        inserted = await store.save_online(
            first_checkpoint,
            ProjectionExpectation(),
        )
        advanced = await store.save_online(
            second_checkpoint,
            ProjectionExpectation(generation=1, last_journal_sequence=1),
        )
        rebuilt = await store.replace_rebuild(
            second_checkpoint,
            ProjectionExpectation(generation=1, last_journal_sequence=2),
        )

        assert inserted.generation == 1
        assert advanced.checkpoint.last_journal_sequence == 2
        assert rebuilt.generation == 2
        assert "ON CONFLICT" in insert_session.statements[0][0]
        assert "generation = :expected_generation" in (
            advance_session.statements[0][0]
        )
        assert "generation = generation + 1" in rebuild_session.statements[0][0]

    asyncio.run(scenario())


def test_load_and_unhealthy_transition_round_trip() -> None:
    async def scenario() -> None:
        projection_checkpoint = checkpoint(2, 2)
        load_session = RecordingSession(
            stored_row(projection_checkpoint, generation=1)
        )
        unhealthy_session = RecordingSession(
            stored_row(
                projection_checkpoint,
                generation=1,
                health="diverged",
                failure_code="replay_divergence",
            )
        )
        transactions = TransactionQueue(load_session, unhealthy_session)
        store = PostgresProjectionStore(transactions.transaction)

        loaded = await store.load(WORKSPACE_ID, PROJECTION_NAME)
        unhealthy = await store.mark_unhealthy(
            WORKSPACE_ID,
            PROJECTION_NAME,
            ProjectionHealth.DIVERGED,
            "replay_divergence",
            ProjectionExpectation(generation=1, last_journal_sequence=2),
        )

        assert loaded is not None
        assert loaded.health is ProjectionHealth.HEALTHY
        assert unhealthy.health is ProjectionHealth.DIVERGED
        assert unhealthy.failure_code == "replay_divergence"

    asyncio.run(scenario())


def test_stale_write_and_invalid_storage_record_fail_safely() -> None:
    async def scenario() -> None:
        conflict_session = RecordingSession(None)
        invalid_session = RecordingSession(
            stored_row(
                checkpoint(1, 1),
                generation=1,
                updated_at="not-a-datetime",
            )
        )
        transactions = TransactionQueue(conflict_session, invalid_session)
        store = PostgresProjectionStore(transactions.transaction)

        with pytest.raises(ProjectionStoreConflict):
            await store.save_online(
                checkpoint(1, 1),
                ProjectionExpectation(),
            )
        with pytest.raises(
            JournalStorageError,
            match="projection timestamp is invalid",
        ):
            await store.load(WORKSPACE_ID, PROJECTION_NAME)

    asyncio.run(scenario())
