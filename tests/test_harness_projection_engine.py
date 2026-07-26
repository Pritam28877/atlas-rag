import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.journal import JournalEvent
from app.services.harness.journal.projection_contracts import (
    ProjectionCheckpoint,
    ProjectionDefinition,
    ProjectionError,
    ProjectionErrorCode,
)
from app.services.harness.journal.projection_engine import (
    advance_projection,
    require_equivalent_projection,
)
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    StrictProtocolModel,
    TraceLink,
)

NOW = datetime(2026, 7, 26, 21, 0, tzinfo=UTC)
WORKSPACE_ID = "wsp_" + "1" * 32


class CounterState(StrictProtocolModel):
    accepted: int
    event_types: tuple[str, ...]


def identifier(prefix: str, number: int = 0) -> str:
    return f"{prefix}_{number:032x}"


def journal_event(sequence: int) -> JournalEvent:
    text_value = f"projection-event-{sequence}"
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


def reduce_counter(state: CounterState, event: JournalEvent) -> CounterState:
    return CounterState(
        accepted=state.accepted + 1,
        event_types=(*state.event_types, event.event.event_type),
    )


def definition(
    *,
    reducer=reduce_counter,
    version: str = "1.0",
) -> ProjectionDefinition[CounterState]:
    return ProjectionDefinition(
        name="turn_counter",
        version=version,
        state_model=CounterState,
        initial_state=CounterState(accepted=0, event_types=()),
        reducer=reducer,
    )


def test_incremental_and_golden_replay_hashes_match() -> None:
    events = tuple(journal_event(sequence) for sequence in range(1, 4))

    online = advance_projection(definition(), WORKSPACE_ID, events[:2])
    online = advance_projection(
        definition(),
        WORKSPACE_ID,
        events[2:],
        checkpoint=online,
    )
    rebuilt = advance_projection(definition(), WORKSPACE_ID, events)

    require_equivalent_projection(online, rebuilt)
    assert online.state_json == (
        '{"accepted":3,"event_types":'
        '["Turn.Accepted","Turn.Accepted","Turn.Accepted"]}'
    )
    assert online.last_journal_sequence == 3
    assert online.event_count == 3


def test_checkpoint_hash_and_json_are_canonical() -> None:
    checkpoint = advance_projection(
        definition(),
        WORKSPACE_ID,
        (journal_event(1),),
    )
    values = checkpoint.model_dump()

    values["state_json"] = '{ "accepted": 1, "event_types": ["Turn.Accepted"] }'
    values["state_sha256"] = hashlib.sha256(
        values["state_json"].encode()
    ).hexdigest()
    with pytest.raises(ValidationError, match="canonical"):
        ProjectionCheckpoint.model_validate(values)

    values = checkpoint.model_dump()
    values["state_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="hash mismatch"):
        ProjectionCheckpoint.model_validate(values)


def test_replay_fails_closed_on_order_version_and_reducer_errors() -> None:
    checkpoint = advance_projection(
        definition(),
        WORKSPACE_ID,
        (journal_event(2),),
    )
    with pytest.raises(ProjectionError) as order_failure:
        advance_projection(
            definition(),
            WORKSPACE_ID,
            (journal_event(2),),
            checkpoint=checkpoint,
        )
    assert order_failure.value.code is ProjectionErrorCode.EVENT_ORDER

    with pytest.raises(ProjectionError) as version_failure:
        advance_projection(
            definition(version="1.1"),
            WORKSPACE_ID,
            (),
            checkpoint=checkpoint,
        )
    assert version_failure.value.code is ProjectionErrorCode.CHECKPOINT_MISMATCH

    def failing_reducer(
        state: CounterState,
        event: JournalEvent,
    ) -> CounterState:
        raise RuntimeError(f"private payload: {event.event.payload}")

    with pytest.raises(ProjectionError) as reducer_failure:
        advance_projection(
            definition(reducer=failing_reducer),
            WORKSPACE_ID,
            (journal_event(1),),
        )
    assert reducer_failure.value.code is ProjectionErrorCode.REDUCER_FAILURE
    assert "private payload" not in str(reducer_failure.value)


def test_divergence_and_page_overflow_are_detected() -> None:
    events = tuple(journal_event(sequence) for sequence in range(1, 3))
    expected = advance_projection(definition(), WORKSPACE_ID, events)

    def divergent_reducer(
        state: CounterState,
        event: JournalEvent,
    ) -> CounterState:
        return CounterState(
            accepted=state.accepted + 2,
            event_types=(*state.event_types, event.event.event_type),
        )

    divergent = advance_projection(
        definition(reducer=divergent_reducer),
        WORKSPACE_ID,
        events,
    )
    with pytest.raises(ProjectionError) as divergence:
        require_equivalent_projection(expected, divergent)
    assert divergence.value.code is ProjectionErrorCode.DIVERGENCE

    oversized_page = tuple(journal_event(sequence) for sequence in range(1, 258))
    with pytest.raises(ProjectionError) as overflow:
        advance_projection(definition(), WORKSPACE_ID, oversized_page)
    assert overflow.value.code is ProjectionErrorCode.PAGE_LIMIT
