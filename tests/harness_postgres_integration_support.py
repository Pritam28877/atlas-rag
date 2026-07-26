"""Shared setup for opt-in PostgreSQL harness integration tests."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.services.harness.journal import (
    AppendRequest,
    JournalDurability,
    JournalEvent,
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECTION_NAME = "durability_counter"


class CountState(StrictProtocolModel):
    count: int


def count_projection() -> ProjectionDefinition[CountState]:
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


def prepare_database(monkeypatch: pytest.MonkeyPatch) -> str:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    parsed_url = make_url(database_url)
    if parsed_url.database is None or not parsed_url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")
    monkeypatch.setenv("DATABASE__URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")
    return database_url


def append_request() -> AppendRequest:
    workspace_id = f"wsp_{uuid4().hex}"
    aggregate_id = f"trn_{uuid4().hex}"
    event_id = f"evt_{uuid4().hex}"
    principal_id = f"prn_{uuid4().hex}"
    request_id = f"req_{uuid4().hex}"
    correlation_id = f"evt_{uuid4().hex}"
    text_value = "postgres-durability-event"
    encoded_value = text_value.encode()
    event = EventRecord(
        event_id=event_id,
        event_type="Turn.Accepted",
        schema_version="1.2",
        aggregate_id=aggregate_id,
        aggregate_sequence=1,
        actor_kind=EventActorKind.SYSTEM,
        actor_principal_id=principal_id,
        occurred_at=datetime.now(UTC),
        trace=TraceLink(
            request_id=request_id,
            correlation_id=correlation_id,
        ),
        payload=InlinePayload(
            text=text_value,
            size_bytes=len(encoded_value),
            content_sha256=hashlib.sha256(encoded_value).hexdigest(),
        ),
    )
    return AppendRequest(
        workspace_id=workspace_id,
        aggregate_id=aggregate_id,
        expected_sequence=0,
        idempotency_key=f"postgres-durability-{uuid4().hex}",
        request_sha256=uuid4().hex * 2,
        durability=JournalDurability.SYNCHRONOUS,
        events=(event,),
    )
