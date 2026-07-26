"""Atomic online projection application within a SQLite append."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.services.harness.journal.contracts import JournalEvent
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.projection_contracts import ProjectionDefinition
from app.services.harness.journal.projection_engine import (
    advance_projection,
    initial_checkpoint,
)
from app.services.harness.journal.projection_store import ProjectionHealth
from app.services.harness.journal.sqlite_projection_rows import (
    stored_projection_from_row,
)
from app.services.harness.journal.sqlite_projection_sql import (
    ADVANCE_PROJECTION,
    INSERT_PROJECTION,
    LOAD_PROJECTION,
)


def apply_online_projections(
    connection: sqlite3.Connection,
    definitions: tuple[ProjectionDefinition[Any], ...],
    workspace_id: str,
    events: tuple[JournalEvent, ...],
    *,
    prior_journal_sequence: int,
    updated_at: datetime,
) -> None:
    for definition in definitions:
        parameters = {
            "workspace_id": workspace_id,
            "projection_name": definition.name,
        }
        row = connection.execute(LOAD_PROJECTION, parameters).fetchone()
        if row is None:
            if prior_journal_sequence != 0:
                raise JournalStorageError(
                    "online projection requires a complete rebuild"
                )
            checkpoint = initial_checkpoint(definition, workspace_id)
            expected_generation = None
        else:
            stored = stored_projection_from_row(row)
            if stored.health is not ProjectionHealth.HEALTHY:
                raise JournalStorageError("online projection is unhealthy")
            checkpoint = stored.checkpoint
            expected_generation = stored.generation
            if checkpoint.last_journal_sequence != prior_journal_sequence:
                raise JournalStorageError(
                    "online projection checkpoint is not current"
                )
        next_checkpoint = advance_projection(
            definition,
            workspace_id,
            events,
            checkpoint=checkpoint,
        )
        write_parameters: dict[str, object] = {
            "workspace_id": workspace_id,
            "projection_name": definition.name,
            "projection_version": definition.version,
            "last_journal_sequence": next_checkpoint.last_journal_sequence,
            "event_count": next_checkpoint.event_count,
            "state_json": next_checkpoint.state_json,
            "state_sha256": next_checkpoint.state_sha256,
            "updated_at": updated_at.isoformat(),
        }
        statement = INSERT_PROJECTION
        if expected_generation is not None:
            statement = ADVANCE_PROJECTION
            write_parameters["expected_generation"] = expected_generation
            write_parameters["expected_sequence"] = prior_journal_sequence
        written = connection.execute(statement, write_parameters).fetchone()
        if written is None:
            raise JournalStorageError("online projection write conflicted")
