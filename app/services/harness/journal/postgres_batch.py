"""Bounded parameterized event batches for the PostgreSQL journal."""

import hashlib
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.harness.journal.contracts import AppendRequest
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.postgres_sql import INSERT_EVENTS_PREFIX


async def insert_event_batch(
    session: AsyncSession,
    request: AppendRequest,
    committed_at: datetime,
    journal_sequences: tuple[int, ...],
) -> None:
    value_rows: list[str] = []
    parameters: dict[str, object] = {
        "workspace_id": request.workspace_id,
        "aggregate_id": request.aggregate_id,
        "request_sha256": request.request_sha256,
        "durability": request.durability.value,
        "committed_at": committed_at,
    }
    for event_index, (event, journal_sequence) in enumerate(
        zip(request.events, journal_sequences, strict=True)
    ):
        event_json = event.model_dump_json()
        parameters[f"event_id_{event_index}"] = event.event_id
        parameters[f"journal_sequence_{event_index}"] = journal_sequence
        parameters[f"aggregate_sequence_{event_index}"] = event.aggregate_sequence
        parameters[f"event_json_{event_index}"] = event_json
        parameters[f"event_sha256_{event_index}"] = hashlib.sha256(
            event_json.encode()
        ).hexdigest()
        value_rows.append(
            "("
            f":journal_sequence_{event_index}, :event_id_{event_index}, "
            ":workspace_id, :aggregate_id, "
            f":aggregate_sequence_{event_index}, :event_json_{event_index}, "
            f":event_sha256_{event_index}, :request_sha256, :durability, "
            ":committed_at)"
        )
    statement = (
        f"{INSERT_EVENTS_PREFIX}{','.join(value_rows)} "
        "RETURNING journal_sequence, aggregate_sequence"
    )
    inserted_rows = (await session.execute(text(statement), parameters)).mappings()
    inserted_pairs = sorted(
        (
            int(row["aggregate_sequence"]),
            int(row["journal_sequence"]),
        )
        for row in inserted_rows
    )
    expected_sequences = tuple(event.aggregate_sequence for event in request.events)
    actual_aggregate_sequences = tuple(pair[0] for pair in inserted_pairs)
    actual_journal_sequences = tuple(pair[1] for pair in inserted_pairs)
    if (
        actual_aggregate_sequences != expected_sequences
        or actual_journal_sequences != journal_sequences
    ):
        raise JournalStorageError("journal insert result is incomplete")
