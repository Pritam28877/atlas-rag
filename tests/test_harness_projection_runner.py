import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.journal import (
    AppendRequest,
    AppendResult,
    GlobalJournalPage,
    GlobalJournalReadRequest,
    JournalEvent,
    JournalPage,
    JournalReadRequest,
)
from app.services.harness.journal.projection_contracts import ProjectionDefinition
from app.services.harness.journal.projection_engine import advance_projection
from app.services.harness.journal.projection_runner import (
    ProjectionRunError,
    ProjectionRunErrorCode,
    ProjectionRunner,
)
from app.services.harness.journal.projection_store import (
    ProjectionExpectation,
    ProjectionHealth,
    ProjectionStoreConflict,
    StoredProjection,
)
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    StrictProtocolModel,
    TraceLink,
)

NOW = datetime(2026, 7, 26, 22, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "2" * 32


class CounterState(StrictProtocolModel):
    count: int


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def journal_event(sequence: int) -> JournalEvent:
    text_value = f"runner-event-{sequence}"
    encoded_value = text_value.encode()
    return JournalEvent(
        journal_sequence=sequence,
        event=EventRecord(
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
        ),
    )


def definition(*, increment: int = 1) -> ProjectionDefinition[CounterState]:
    def reducer(state: CounterState, event: JournalEvent) -> CounterState:
        _ = event
        return CounterState(count=state.count + increment)

    return ProjectionDefinition(
        name="runner_counter",
        version="1.0",
        state_model=CounterState,
        initial_state=CounterState(count=0),
        reducer=reducer,
    )


class FakeJournal:
    def __init__(self, events: tuple[JournalEvent, ...]) -> None:
        self.events = events

    async def append(self, request: AppendRequest) -> AppendResult:
        raise AssertionError(f"unexpected append: {request.aggregate_id}")

    async def read_aggregate(self, request: JournalReadRequest) -> JournalPage:
        raise AssertionError(f"unexpected aggregate read: {request.aggregate_id}")

    async def read_global(
        self,
        request: GlobalJournalReadRequest,
    ) -> GlobalJournalPage:
        remaining = tuple(
            event
            for event in self.events
            if event.journal_sequence > request.after_journal_sequence
        )
        page_events = remaining[: request.limit]
        return GlobalJournalPage(
            workspace_id=request.workspace_id,
            after_journal_sequence=request.after_journal_sequence,
            events=page_events,
            has_more=len(remaining) > request.limit,
        )


class FakeStore:
    def __init__(self, record: StoredProjection | None = None) -> None:
        self.record = record
        self.force_conflict = False
        self.online_writes = 0
        self.rebuild_writes = 0

    async def load(self, workspace_id: str, projection_name: str):
        if self.record is None:
            return None
        if (
            self.record.checkpoint.workspace_id != workspace_id
            or self.record.checkpoint.projection_name != projection_name
        ):
            return None
        return self.record

    async def save_online(
        self,
        checkpoint,
        expected: ProjectionExpectation,
    ) -> StoredProjection:
        self._require_expectation(expected)
        generation = 1 if self.record is None else self.record.generation
        self.record = StoredProjection(
            generation=generation,
            checkpoint=checkpoint,
            health=ProjectionHealth.HEALTHY,
            updated_at=NOW,
        )
        self.online_writes += 1
        return self.record

    async def replace_rebuild(
        self,
        checkpoint,
        expected: ProjectionExpectation,
    ) -> StoredProjection:
        self._require_expectation(expected)
        generation = 1 if self.record is None else self.record.generation + 1
        self.record = StoredProjection(
            generation=generation,
            checkpoint=checkpoint,
            health=ProjectionHealth.HEALTHY,
            updated_at=NOW,
        )
        self.rebuild_writes += 1
        return self.record

    async def mark_unhealthy(
        self,
        workspace_id: str,
        projection_name: str,
        health: ProjectionHealth,
        failure_code: str,
        expected: ProjectionExpectation,
    ) -> StoredProjection:
        self._require_expectation(expected)
        assert self.record is not None
        assert self.record.checkpoint.workspace_id == workspace_id
        assert self.record.checkpoint.projection_name == projection_name
        self.record = self.record.model_copy(
            update={
                "health": health,
                "failure_code": failure_code,
                "updated_at": NOW,
            }
        )
        return self.record

    def _require_expectation(self, expected: ProjectionExpectation) -> None:
        if self.force_conflict:
            raise ProjectionStoreConflict
        actual_generation = None if self.record is None else self.record.generation
        actual_sequence = (
            None
            if self.record is None
            else self.record.checkpoint.last_journal_sequence
        )
        if (
            actual_generation != expected.generation
            or actual_sequence != expected.last_journal_sequence
        ):
            raise ProjectionStoreConflict


def events() -> tuple[JournalEvent, ...]:
    return tuple(journal_event(sequence) for sequence in range(1, 4))


def test_catch_up_is_restartable_and_matches_complete_rebuild() -> None:
    async def scenario() -> None:
        store = FakeStore()
        runner = ProjectionRunner(FakeJournal(events()), store, page_size=2)

        partial = await runner.catch_up(
            definition(),
            WORKSPACE_ID,
            maximum_pages=1,
        )
        assert partial.has_more
        assert partial.checkpoint.last_journal_sequence == 2

        completed = await runner.catch_up(definition(), WORKSPACE_ID)
        rebuilt = await runner.rebuild(definition(), WORKSPACE_ID)

        assert not completed.has_more
        assert rebuilt.checkpoint.state_sha256 == completed.checkpoint.state_sha256
        assert rebuilt.generation == 2
        assert store.online_writes == 2
        assert store.rebuild_writes == 1

    asyncio.run(scenario())


def test_incomplete_rebuild_is_never_published() -> None:
    async def scenario() -> None:
        store = FakeStore()
        runner = ProjectionRunner(FakeJournal(events()), store, page_size=1)

        with pytest.raises(ProjectionRunError) as failure:
            await runner.rebuild(
                definition(),
                WORKSPACE_ID,
                maximum_pages=1,
            )
        assert failure.value.code is ProjectionRunErrorCode.REBUILD_LIMIT
        assert store.record is None
        assert store.rebuild_writes == 0

    asyncio.run(scenario())


def test_divergence_marks_unhealthy_and_rebuild_recovers_next_generation() -> None:
    async def scenario() -> None:
        divergent_checkpoint = advance_projection(
            definition(increment=2),
            WORKSPACE_ID,
            events(),
        )
        store = FakeStore(
            StoredProjection(
                generation=1,
                checkpoint=divergent_checkpoint,
                health=ProjectionHealth.HEALTHY,
                updated_at=NOW,
            )
        )
        runner = ProjectionRunner(FakeJournal(events()), store, page_size=2)

        with pytest.raises(ProjectionRunError) as failure:
            await runner.rebuild(definition(), WORKSPACE_ID)
        assert failure.value.code is ProjectionRunErrorCode.DIVERGENCE
        assert store.record is not None
        assert store.record.health is ProjectionHealth.DIVERGED
        assert store.record.failure_code == "replay_divergence"

        recovered = await runner.rebuild(definition(), WORKSPACE_ID)
        assert recovered.generation == 2
        assert store.record.health is ProjectionHealth.HEALTHY

    asyncio.run(scenario())


def test_stale_checkpoint_write_returns_stable_conflict() -> None:
    async def scenario() -> None:
        store = FakeStore()
        store.force_conflict = True
        runner = ProjectionRunner(FakeJournal(events()), store)

        with pytest.raises(ProjectionRunError) as failure:
            await runner.catch_up(definition(), WORKSPACE_ID)
        assert failure.value.code is ProjectionRunErrorCode.CONFLICT

    asyncio.run(scenario())
