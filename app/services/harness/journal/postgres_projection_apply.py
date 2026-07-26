"""Atomic online projection application within a journal transaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.harness.journal.contracts import JournalEvent
from app.services.harness.journal.errors import JournalStorageError
from app.services.harness.journal.postgres_projection_rows import (
    stored_projection_from_row,
)
from app.services.harness.journal.postgres_projection_sql import (
    ADVANCE_PROJECTION,
    INSERT_PROJECTION,
    LOCK_PROJECTION,
)
from app.services.harness.journal.projection_contracts import ProjectionDefinition
from app.services.harness.journal.projection_engine import (
    advance_projection,
    initial_checkpoint,
)
from app.services.harness.journal.projection_store import ProjectionHealth

MAXIMUM_ONLINE_PROJECTIONS = 32


def prepare_online_projections(
    definitions: Sequence[ProjectionDefinition[Any]],
) -> tuple[ProjectionDefinition[Any], ...]:
    if len(definitions) > MAXIMUM_ONLINE_PROJECTIONS:
        raise ValueError("online projection count exceeds 32")
    ordered = tuple(sorted(definitions, key=lambda definition: definition.name))
    names = tuple(definition.name for definition in ordered)
    if len(names) != len(set(names)):
        raise ValueError("online projection names must be unique")
    return ordered


async def apply_online_projections(
    session: AsyncSession,
    definitions: tuple[ProjectionDefinition[Any], ...],
    workspace_id: str,
    events: tuple[JournalEvent, ...],
) -> None:
    if not definitions:
        return
    prior_journal_sequence = events[0].journal_sequence - 1
    for definition in definitions:
        parameters = {
            "workspace_id": workspace_id,
            "projection_name": definition.name,
        }
        row = (
            await session.execute(text(LOCK_PROJECTION), parameters)
        ).mappings().one_or_none()
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
        }
        statement = INSERT_PROJECTION
        if expected_generation is not None:
            statement = ADVANCE_PROJECTION
            write_parameters["expected_generation"] = expected_generation
            write_parameters["expected_sequence"] = prior_journal_sequence
        written = (
            await session.execute(text(statement), write_parameters)
        ).mappings().one_or_none()
        if written is None:
            raise JournalStorageError("online projection write conflicted")
