import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.database import Database
from app.services.harness.journal.postgres_projection_store import (
    PostgresProjectionStore,
)
from app.services.harness.journal.projection_contracts import ProjectionCheckpoint
from app.services.harness.journal.projection_store import (
    ProjectionExpectation,
    ProjectionHealth,
    ProjectionStoreConflict,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def checkpoint(
    workspace_id: str,
    projection_name: str,
    sequence: int,
    count: int,
) -> ProjectionCheckpoint:
    state_json = f'{{"count":{count}}}'
    return ProjectionCheckpoint(
        workspace_id=workspace_id,
        projection_name=projection_name,
        projection_version="1.0",
        last_journal_sequence=sequence,
        event_count=count,
        state_json=state_json,
        state_sha256=hashlib.sha256(state_json.encode()).hexdigest(),
    )


@pytest.mark.database_integration
def test_postgres_projection_store_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    parsed_url = make_url(database_url)
    if parsed_url.database is None or not parsed_url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")

    monkeypatch.setenv("DATABASE__URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")

    async def scenario() -> None:
        database = Database(get_settings().database)
        store = PostgresProjectionStore(database.transaction)
        workspace_id = f"wsp_{uuid4().hex}"
        projection_name = f"projection_{uuid4().hex}"
        first = checkpoint(workspace_id, projection_name, 1, 1)
        second = checkpoint(workspace_id, projection_name, 2, 2)
        alternate = checkpoint(workspace_id, projection_name, 3, 3)
        try:
            inserted = await store.save_online(first, ProjectionExpectation())
            outcomes = await asyncio.gather(
                store.save_online(
                    second,
                    ProjectionExpectation(
                        generation=1,
                        last_journal_sequence=1,
                    ),
                ),
                store.save_online(
                    alternate,
                    ProjectionExpectation(
                        generation=1,
                        last_journal_sequence=1,
                    ),
                ),
                return_exceptions=True,
            )
            loaded = await store.load(workspace_id, projection_name)
            assert loaded is not None
            unhealthy = await store.mark_unhealthy(
                workspace_id,
                projection_name,
                ProjectionHealth.DIVERGED,
                "integration_divergence",
                ProjectionExpectation(
                    generation=loaded.generation,
                    last_journal_sequence=loaded.checkpoint.last_journal_sequence,
                ),
            )
            rebuilt = await store.replace_rebuild(
                loaded.checkpoint,
                ProjectionExpectation(
                    generation=unhealthy.generation,
                    last_journal_sequence=unhealthy.checkpoint.last_journal_sequence,
                ),
            )
        finally:
            await database.close()

        assert inserted.generation == 1
        assert sum(
            isinstance(outcome, ProjectionStoreConflict)
            for outcome in outcomes
        ) == 1
        assert unhealthy.health is ProjectionHealth.DIVERGED
        assert rebuilt.generation == 2
        assert rebuilt.health is ProjectionHealth.HEALTHY

    try:
        asyncio.run(scenario())
    finally:
        get_settings.cache_clear()
