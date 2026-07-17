import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from prometheus_client import CollectorRegistry
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from app.core.config import DatabaseSettings, Settings, TelemetrySettings, get_settings
from app.core.database import Database, normalize_database_url
from app.core.readiness import InfrastructureReadiness
from app.core.telemetry import Telemetry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT_NAME = "document-loader-reconciliation"


def _database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL")
    if value is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    if not value.rsplit("/", 1)[-1].endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")
    return value


@pytest.mark.database_integration
def test_database_metrics_and_scheduler_readiness_use_real_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    monkeypatch.setenv("DATABASE__URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")
    settings = Settings(database=DatabaseSettings(url=SecretStr(database_url)))
    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    database = Database(settings.database)
    telemetry = Telemetry(
        TelemetrySettings(metrics_cache_ttl_seconds=1),
        registry=CollectorRegistry(),
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO service_heartbeats (service_name, updated_at)
                    VALUES (:name, now())
                    ON CONFLICT (service_name)
                    DO UPDATE SET updated_at = EXCLUDED.updated_at
                    """
                ),
                {"name": HEARTBEAT_NAME},
            )

        asyncio.run(telemetry.refresh_from_database(database))
        metrics = telemetry.metrics().decode("utf-8")
        assert "rag_queue_metrics_refresh_success 1.0" in metrics
        assert "rag_scheduler_heartbeat_present 1.0" in metrics
        asyncio.run(InfrastructureReadiness(settings)._check_scheduler())

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE service_heartbeats "
                    "SET updated_at = now() - interval '1 hour' "
                    "WHERE service_name = :name"
                ),
                {"name": HEARTBEAT_NAME},
            )
        with pytest.raises(RuntimeError, match="heartbeat is stale"):
            asyncio.run(InfrastructureReadiness(settings)._check_scheduler())
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM service_heartbeats WHERE service_name = :name"),
                {"name": HEARTBEAT_NAME},
            )
        asyncio.run(database.close())
        telemetry.close()
        engine.dispose()
        get_settings.cache_clear()
