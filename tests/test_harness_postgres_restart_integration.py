import asyncio
import os
import re
import subprocess

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.database import Database
from app.services.harness.journal import GlobalJournalReadRequest
from app.services.harness.journal.postgres import PostgresEventJournal
from app.services.harness.journal.postgres_projection_store import (
    PostgresProjectionStore,
)
from tests.harness_postgres_integration_support import (
    PROJECTION_NAME,
    append_request,
    count_projection,
    prepare_database,
)

CONTAINER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
PUBLISHED_PORT_PATTERN = re.compile(r"^127\.0\.0\.1:([0-9]{1,5})$")


def _container_name() -> str:
    container_name = os.environ.get("TEST_POSTGRES_CONTAINER")
    if container_name is None:
        pytest.skip("TEST_POSTGRES_CONTAINER is not configured")
    if CONTAINER_NAME_PATTERN.fullmatch(container_name) is None:
        pytest.fail("TEST_POSTGRES_CONTAINER is invalid")
    label = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "com.atlas.harness-test"}}',
            container_name,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if label.returncode != 0 or label.stdout.strip() != "true":
        pytest.fail("PostgreSQL container lacks the harness-test label")
    return container_name


def _published_port(container_name: str) -> int:
    published = subprocess.run(
        ["docker", "port", container_name, "5432/tcp"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    match = PUBLISHED_PORT_PATTERN.fullmatch(published.stdout.strip())
    if published.returncode != 0 or match is None:
        pytest.fail("PostgreSQL container must publish 5432 on loopback")
    port = int(match.group(1))
    if not 1 <= port <= 65_535:
        pytest.fail("PostgreSQL container port is invalid")
    return port


async def wait_for_database() -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    while True:
        database = Database(get_settings().database)
        try:
            await database.check_connection()
            return
        except SQLAlchemyError:
            if loop.time() >= deadline:
                raise
            await asyncio.sleep(0.25)
        finally:
            await database.close()


@pytest.mark.database_integration
def test_synchronous_append_survives_postgres_container_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = prepare_database(monkeypatch)
    container_name = _container_name()
    parsed_url = make_url(database_url)
    if (
        parsed_url.host != "127.0.0.1"
        or parsed_url.port != _published_port(container_name)
    ):
        pytest.fail("PostgreSQL container does not match TEST_DATABASE_URL")
    request = append_request()

    async def append() -> None:
        database = Database(get_settings().database)
        journal = PostgresEventJournal(
            database.transaction,
            projections=(count_projection(),),
        )
        try:
            await journal.append(request)
        finally:
            await database.close()

    async def verify() -> None:
        database = Database(get_settings().database)
        journal = PostgresEventJournal(database.transaction)
        projection_store = PostgresProjectionStore(database.transaction)
        try:
            page = await journal.read_global(
                GlobalJournalReadRequest(
                    workspace_id=request.workspace_id,
                    after_journal_sequence=0,
                )
            )
            projection = await projection_store.load(
                request.workspace_id,
                PROJECTION_NAME,
            )
        finally:
            await database.close()

        assert len(page.events) == 1
        assert page.events[0].event.event_id == request.events[0].event_id
        assert projection is not None
        assert projection.checkpoint.state_json == '{"count":1}'

    try:
        asyncio.run(append())
        restarted = subprocess.run(
            ["docker", "restart", "--time", "5", container_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert restarted.returncode == 0
        restarted_port = _published_port(container_name)
        restarted_url = parsed_url.set(port=restarted_port).render_as_string(
            hide_password=False
        )
        monkeypatch.setenv("DATABASE__URL", restarted_url)
        get_settings.cache_clear()
        asyncio.run(wait_for_database())
        asyncio.run(verify())
    finally:
        get_settings.cache_clear()
