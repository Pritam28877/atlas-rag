"""Terminate a child process at one explicit SQLite journal boundary."""

import argparse
import asyncio
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from app.services.harness.journal import (
    AppendRequest,
    JournalDurability,
    JournalEvent,
)
from app.services.harness.journal.faults import JournalFaultPoint
from app.services.harness.journal.projection_contracts import ProjectionDefinition
from app.services.harness.journal.sqlite import SQLiteEventJournal
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    StrictProtocolModel,
    TraceLink,
)

KILL_EXIT_CODE = 97
PROJECTION_NAME = "kill_probe_counter"
NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)


class CountState(StrictProtocolModel):
    count: int


def projection() -> ProjectionDefinition[CountState]:
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


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def append_request() -> AppendRequest:
    text_value = "kill-probe-event"
    encoded_value = text_value.encode()
    event = EventRecord(
        event_id=identifier("evt", 1),
        event_type="Turn.Accepted",
        schema_version="1.2",
        aggregate_id=identifier("trn"),
        aggregate_sequence=1,
        actor_kind=EventActorKind.SYSTEM,
        actor_principal_id=identifier("prn"),
        occurred_at=NOW,
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
    return AppendRequest(
        workspace_id=identifier("wsp"),
        aggregate_id=identifier("trn"),
        expected_sequence=0,
        idempotency_key="sqlite-kill-probe-command",
        request_sha256="9" * 64,
        durability=JournalDurability.SYNCHRONOUS,
        events=(event,),
    )


async def run_probe(
    database_path: Path,
    target_fault_point: JournalFaultPoint,
) -> None:
    def terminate(fault_point: JournalFaultPoint) -> None:
        if fault_point is target_fault_point:
            os._exit(KILL_EXIT_CODE)

    journal = await SQLiteEventJournal.open(
        database_path,
        clock=lambda: NOW,
        projections=(projection(),),
        fault_injector=terminate,
    )
    try:
        await journal.append(append_request())
    finally:
        await journal.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument(
        "--fault-point",
        required=True,
        choices=tuple(point.value for point in JournalFaultPoint),
    )
    arguments = parser.parse_args()
    asyncio.run(
        run_probe(
            arguments.database_path,
            JournalFaultPoint(arguments.fault_point),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
