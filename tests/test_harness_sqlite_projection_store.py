import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.journal import (
    AppendRequest,
    JournalDurability,
    JournalEvent,
    SQLiteEventJournal,
)
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.projection_contracts import (
    ProjectionCheckpoint,
    ProjectionDefinition,
)
from app.services.harness.journal.projection_runner import ProjectionRunner
from app.services.harness.journal.projection_store import (
    ProjectionExpectation,
    ProjectionHealth,
    ProjectionStoreConflict,
)
from app.services.harness.journal.sqlite_projection_store import (
    SQLiteProjectionStore,
)
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    StrictProtocolModel,
    TraceLink,
)

NOW = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "4" * 32
PROJECTION_NAME = "sqlite_counter"


class CountState(StrictProtocolModel):
    count: int


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def event(sequence: int) -> EventRecord:
    text_value = f"sqlite-projection-{sequence}"
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
        workspace_id=WORKSPACE_ID,
        aggregate_id=identifier("trn"),
        expected_sequence=0,
        idempotency_key="sqlite-projection-command",
        request_sha256="e" * 64,
        durability=JournalDurability.SYNCHRONOUS,
        events=events,
    )


def definition() -> ProjectionDefinition[CountState]:
    def reducer(state: CountState, journal_event: JournalEvent) -> CountState:
        _ = journal_event
        return CountState(count=state.count + 1)

    return ProjectionDefinition(
        name=PROJECTION_NAME,
        version="1.0",
        state_model=CountState,
        initial_state=CountState(count=0),
        reducer=reducer,
    )


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


def database_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "journal.sqlite3"


def test_runner_persists_resumes_and_rebuilds_sqlite_projection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        journal = await SQLiteEventJournal.open(path, clock=lambda: NOW)
        store = await SQLiteProjectionStore.open(path, clock=lambda: NOW)
        await journal.append(append_request(event(1), event(2), event(3)))
        runner = ProjectionRunner(journal, store, page_size=2)

        partial = await runner.catch_up(
            definition(),
            WORKSPACE_ID,
            maximum_pages=1,
        )
        completed = await runner.catch_up(definition(), WORKSPACE_ID)
        rebuilt = await runner.rebuild(definition(), WORKSPACE_ID)
        await store.close()

        reopened = await SQLiteProjectionStore.open(path)
        restored = await reopened.load(WORKSPACE_ID, PROJECTION_NAME)
        await reopened.close()
        await journal.close()

        assert partial.has_more
        assert completed.checkpoint.state_json == '{"count":3}'
        assert rebuilt.generation == 2
        assert rebuilt.checkpoint.state_sha256 == completed.checkpoint.state_sha256
        assert restored is not None
        assert restored.generation == 2
        assert restored.checkpoint == rebuilt.checkpoint

    asyncio.run(scenario())


def test_concurrent_sqlite_cas_has_one_winner(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = database_path(tmp_path)
        first_store = await SQLiteProjectionStore.open(path, clock=lambda: NOW)
        second_store = await SQLiteProjectionStore.open(path, clock=lambda: NOW)
        await first_store.save_online(checkpoint(1, 1), ProjectionExpectation())

        outcomes = await asyncio.gather(
            first_store.save_online(
                checkpoint(2, 2),
                ProjectionExpectation(generation=1, last_journal_sequence=1),
            ),
            second_store.save_online(
                checkpoint(3, 3),
                ProjectionExpectation(generation=1, last_journal_sequence=1),
            ),
            return_exceptions=True,
        )
        await first_store.close()
        await second_store.close()

        assert sum(
            isinstance(outcome, ProjectionStoreConflict)
            for outcome in outcomes
        ) == 1

    asyncio.run(scenario())


def test_unhealthy_projection_requires_next_generation_rebuild(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = await SQLiteProjectionStore.open(
            database_path(tmp_path),
            clock=lambda: NOW,
        )
        stored = await store.save_online(checkpoint(1, 1), ProjectionExpectation())
        unhealthy = await store.mark_unhealthy(
            WORKSPACE_ID,
            PROJECTION_NAME,
            ProjectionHealth.DIVERGED,
            "replay_divergence",
            ProjectionExpectation(
                generation=stored.generation,
                last_journal_sequence=1,
            ),
        )
        with pytest.raises(ProjectionStoreConflict):
            await store.save_online(
                checkpoint(2, 2),
                ProjectionExpectation(generation=1, last_journal_sequence=1),
            )
        rebuilt = await store.replace_rebuild(
            checkpoint(1, 1),
            ProjectionExpectation(generation=1, last_journal_sequence=1),
        )
        with pytest.raises(JournalStorageError):
            await store.save_online(
                checkpoint(1, 2),
                ProjectionExpectation(generation=2, last_journal_sequence=1),
            )
        await store.close()

        assert unhealthy.health is ProjectionHealth.DIVERGED
        assert rebuilt.generation == 2
        assert rebuilt.health is ProjectionHealth.HEALTHY

    asyncio.run(scenario())
