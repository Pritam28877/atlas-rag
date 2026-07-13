import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.database import normalize_database_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_migration_graph_has_one_reversible_baseline() -> None:
    config = Config(PROJECT_ROOT / "alembic.ini")
    migrations = ScriptDirectory.from_config(config)

    assert migrations.get_heads() == ["20260713_01"]
    baseline = migrations.get_revision("20260713_01")
    assert baseline is not None
    assert baseline.down_revision is None


def test_migration_paths_resolve_from_project_root() -> None:
    config = Config(PROJECT_ROOT / "alembic.ini")
    migrations = ScriptDirectory.from_config(config)

    assert Path(migrations.dir).resolve() == PROJECT_ROOT / "migrations"


@pytest.mark.database_integration
def test_clean_postgres_migrates_forward_and_rolls_back(monkeypatch) -> None:
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if test_database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    parsed_url = make_url(test_database_url)
    if parsed_url.database is None or not parsed_url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")

    normalized_url = normalize_database_url(test_database_url)
    engine = create_engine(normalized_url, pool_pre_ping=True)
    config = Config(PROJECT_ROOT / "alembic.ini")
    monkeypatch.setenv("DATABASE__URL", test_database_url)
    get_settings.cache_clear()

    try:
        existing_tables = inspect(engine).get_table_names(schema="public")
        assert existing_tables == []

        command.upgrade(config, "head")
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "20260713_01"

        command.downgrade(config, "base")
        with engine.connect() as connection:
            remaining_versions = connection.execute(
                text("SELECT count(*) FROM alembic_version")
            ).scalar_one()
        assert remaining_versions == 0
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        get_settings.cache_clear()
        engine.dispose()
