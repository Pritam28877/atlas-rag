"""Pure deterministic projection reduction and divergence detection."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from pydantic import ValidationError

from app.services.harness.journal.contracts import (
    MAXIMUM_APPEND_EVENTS,
    JournalEvent,
)
from app.services.harness.journal.projection_contracts import (
    ProjectionCheckpoint,
    ProjectionDefinition,
    ProjectionError,
    ProjectionErrorCode,
    StateType,
    canonical_state_json,
)


def initial_checkpoint(
    definition: ProjectionDefinition[StateType],
    workspace_id: str,
) -> ProjectionCheckpoint:
    return _checkpoint(
        definition,
        workspace_id,
        last_journal_sequence=0,
        event_count=0,
        state=definition.initial_state,
    )


def advance_projection(
    definition: ProjectionDefinition[StateType],
    workspace_id: str,
    events: Sequence[JournalEvent],
    *,
    checkpoint: ProjectionCheckpoint | None = None,
) -> ProjectionCheckpoint:
    if len(events) > MAXIMUM_APPEND_EVENTS:
        raise ProjectionError(ProjectionErrorCode.PAGE_LIMIT)
    current_checkpoint = checkpoint or initial_checkpoint(definition, workspace_id)
    state = _restore_state(definition, workspace_id, current_checkpoint)
    previous_sequence = current_checkpoint.last_journal_sequence

    for journal_event in events:
        if journal_event.journal_sequence <= previous_sequence:
            raise ProjectionError(ProjectionErrorCode.EVENT_ORDER)
        try:
            next_state = definition.reducer(state, journal_event)
        except Exception as error:
            raise ProjectionError(ProjectionErrorCode.REDUCER_FAILURE) from error
        if not isinstance(next_state, definition.state_model):
            raise ProjectionError(ProjectionErrorCode.STATE_TYPE)
        state = next_state
        previous_sequence = journal_event.journal_sequence

    return _checkpoint(
        definition,
        workspace_id,
        last_journal_sequence=previous_sequence,
        event_count=current_checkpoint.event_count + len(events),
        state=state,
    )


def require_equivalent_projection(
    online: ProjectionCheckpoint,
    rebuilt: ProjectionCheckpoint,
) -> None:
    identity_matches = (
        online.workspace_id == rebuilt.workspace_id
        and online.projection_name == rebuilt.projection_name
        and online.projection_version == rebuilt.projection_version
        and online.last_journal_sequence == rebuilt.last_journal_sequence
        and online.event_count == rebuilt.event_count
    )
    state_matches = (
        online.state_sha256 == rebuilt.state_sha256
        and online.state_json == rebuilt.state_json
    )
    if not identity_matches or not state_matches:
        raise ProjectionError(ProjectionErrorCode.DIVERGENCE)


def _restore_state(
    definition: ProjectionDefinition[StateType],
    workspace_id: str,
    checkpoint: ProjectionCheckpoint,
) -> StateType:
    if (
        checkpoint.workspace_id != workspace_id
        or checkpoint.projection_name != definition.name
        or checkpoint.projection_version != definition.version
    ):
        raise ProjectionError(ProjectionErrorCode.CHECKPOINT_MISMATCH)
    try:
        return definition.state_model.model_validate_json(checkpoint.state_json)
    except ValidationError as error:
        raise ProjectionError(ProjectionErrorCode.CHECKPOINT_MISMATCH) from error


def _checkpoint(
    definition: ProjectionDefinition[StateType],
    workspace_id: str,
    *,
    last_journal_sequence: int,
    event_count: int,
    state: StateType,
) -> ProjectionCheckpoint:
    try:
        state_json = canonical_state_json(state.model_dump(mode="json"))
        return ProjectionCheckpoint(
            workspace_id=workspace_id,
            projection_name=definition.name,
            projection_version=definition.version,
            last_journal_sequence=last_journal_sequence,
            event_count=event_count,
            state_json=state_json,
            state_sha256=hashlib.sha256(state_json.encode()).hexdigest(),
        )
    except (ValidationError, ValueError) as error:
        raise ProjectionError(ProjectionErrorCode.STATE_TYPE) from error
