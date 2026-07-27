"""Bounded TUI projection and keyboard behavior tests."""

import hashlib
from datetime import UTC, datetime

from app.cli.harness.tui_projection import (
    BoundedTuiProjection,
    TuiActionKind,
    TuiFocus,
    TuiKey,
)
from app.services.harness.protocol import (
    EventActorKind,
    EventRecord,
    InlinePayload,
    TraceLink,
)
from app.services.harness.runtime import ResyncRequired, SequencedEvent

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EVENT_ID = "evt_" + "1" * 32
AGGREGATE_ID = "thr_" + "2" * 32
REQUEST_ID = "req_" + "3" * 32


def _event() -> EventRecord:
    text = "bounded"
    return EventRecord(
        event_id=EVENT_ID,
        event_type="Thread.Created",
        schema_version="1.2",
        aggregate_id=AGGREGATE_ID,
        aggregate_sequence=1,
        actor_kind=EventActorKind.SYSTEM,
        actor_principal_id="prn_" + "4" * 32,
        occurred_at=NOW,
        trace=TraceLink(
            request_id=REQUEST_ID,
            correlation_id=EVENT_ID,
        ),
        payload=InlinePayload(
            text=text,
            size_bytes=len(text),
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
    )


def test_projection_caps_events_and_hides_payload() -> None:
    projection = BoundedTuiProjection(maximum_events=2)
    for sequence in range(1, 4):
        projection.apply_delivery(
            SequencedEvent(journal_sequence=sequence, event=_event())
        )
    snapshot = projection.snapshot()
    assert len(snapshot.events) == 2
    assert all("bounded" not in line for line in projection.render_lines())


def test_keyboard_focus_and_resync_action_are_bounded() -> None:
    projection = BoundedTuiProjection()
    projection.apply_delivery(
        ResyncRequired(
            subscription_id="sub_" + "5" * 32,
            resume_after_sequence=0,
            latest_observed_sequence=4,
        )
    )
    assert projection.snapshot().resync_required
    assert projection.handle_key(TuiKey.RESYNC).kind is TuiActionKind.RESYNC
    assert projection.handle_key(TuiKey.NEXT_PANEL).kind is TuiActionKind.NONE
    assert projection.snapshot().focus is TuiFocus.TASKS
