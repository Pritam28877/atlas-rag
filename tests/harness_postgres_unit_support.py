"""Reusable PostgreSQL journal doubles and deterministic fixtures."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.harness.journal import (
    AppendRequest,
    JournalDurability,
)
from app.services.harness.journal.projection_contracts import (
    ProjectionDefinition,
)
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    StrictProtocolModel,
    TraceLink,
)

NOW = datetime(2026, 7, 26, 20, 0, tzinfo=UTC)


class CountState(StrictProtocolModel):
    count: int


def count_projection() -> ProjectionDefinition[CountState]:
    def reducer(state: CountState, journal_event) -> CountState:
        _ = journal_event
        return CountState(count=state.count + 1)

    return ProjectionDefinition(
        name="append_counter",
        version="1.0",
        state_model=CountState,
        initial_state=CountState(count=0),
        reducer=reducer,
    )


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def event(sequence: int) -> EventRecord:
    text_value = f"event-{sequence}"
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
        workspace_id=identifier("wsp"),
        aggregate_id=identifier("trn"),
        expected_sequence=0,
        idempotency_key="postgres-command-0001",
        request_sha256="a" * 64,
        durability=JournalDurability.SYNCHRONOUS,
        events=events,
    )


class MappingResult:
    def __init__(
        self,
        *,
        one: Mapping[str, object] | None = None,
        rows: tuple[Mapping[str, object], ...] = (),
    ) -> None:
        self._one = one
        self._rows = rows

    def mappings(self) -> MappingResult:
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        return self._one

    def all(self) -> tuple[Mapping[str, object], ...]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class RecordingSession:
    def __init__(
        self,
        *,
        scalar_values: list[object],
        execute_results: list[MappingResult],
        execute_error: SQLAlchemyError | None = None,
    ) -> None:
        self.scalar_values = scalar_values
        self.execute_results = execute_results
        self.execute_error = execute_error
        self.statements: list[tuple[str, Mapping[str, object]]] = []

    async def execute(
        self,
        statement,
        parameters: Mapping[str, object] | None = None,
    ) -> MappingResult:
        self.statements.append((str(statement), parameters or {}))
        if self.execute_error is not None:
            raise self.execute_error
        if self.execute_results:
            return self.execute_results.pop(0)
        return MappingResult()

    async def scalar(
        self,
        statement,
        parameters: Mapping[str, object] | None = None,
    ) -> object:
        self.statements.append((str(statement), parameters or {}))
        return self.scalar_values.pop(0)


class TransactionQueue:
    def __init__(self, *sessions: RecordingSession) -> None:
        self._sessions = list(sessions)
        self.committed = 0
        self.rolled_back = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        session = self._sessions.pop(0)
        try:
            yield cast(AsyncSession, session)
        except BaseException:
            self.rolled_back += 1
            raise
        else:
            self.committed += 1


def append_session() -> RecordingSession:
    return RecordingSession(
        scalar_values=[0, 0, NOW, 2, 2],
        execute_results=[
            MappingResult(),
            MappingResult(),
            MappingResult(),
            MappingResult(),
            MappingResult(
                rows=(
                    {"aggregate_sequence": 1, "journal_sequence": 1},
                    {"aggregate_sequence": 2, "journal_sequence": 2},
                )
            ),
            MappingResult(),
        ],
    )


def projected_append_session() -> RecordingSession:
    return RecordingSession(
        scalar_values=[0, 0, NOW, 2, 2],
        execute_results=[
            MappingResult(),
            MappingResult(),
            MappingResult(),
            MappingResult(),
            MappingResult(
                rows=(
                    {"aggregate_sequence": 1, "journal_sequence": 1},
                    {"aggregate_sequence": 2, "journal_sequence": 2},
                )
            ),
            MappingResult(),
            MappingResult(one={"written": True}),
            MappingResult(),
        ],
    )


def missing_projection_history_session() -> RecordingSession:
    return RecordingSession(
        scalar_values=[0, 2, NOW, 2, 4],
        execute_results=[
            MappingResult(),
            MappingResult(),
            MappingResult(),
            MappingResult(),
            MappingResult(
                rows=(
                    {"aggregate_sequence": 1, "journal_sequence": 3},
                    {"aggregate_sequence": 2, "journal_sequence": 4},
                )
            ),
            MappingResult(),
        ],
    )
