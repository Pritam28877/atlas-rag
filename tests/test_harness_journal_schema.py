import re
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def render_upgrade_sql(monkeypatch) -> str:
    output = StringIO()
    config = Config(PROJECT_ROOT / "alembic.ini", output_buffer=output)
    monkeypatch.setenv(
        "DATABASE__URL",
        "postgresql://journal_test:journal_test@localhost:5432/journal_test",
    )
    get_settings.cache_clear()
    try:
        command.upgrade(config, "head", sql=True)
    finally:
        get_settings.cache_clear()
    return output.getvalue()


def test_journal_schema_is_scoped_bounded_and_replay_safe(monkeypatch) -> None:
    sql = render_upgrade_sql(monkeypatch)
    normalized_sql = re.sub(r"\s+", " ", sql)

    assert "CREATE TABLE harness_journal_positions" in sql
    assert "CREATE TABLE harness_journal_aggregates" in sql
    assert "CREATE TABLE harness_journal_events" in sql
    assert "CREATE TABLE harness_journal_idempotency" in sql
    assert normalized_sql.count("PRIMARY KEY (workspace_id, aggregate_id") == 2
    assert "UNIQUE (workspace_id, event_id)" in normalized_sql
    assert (
        "UNIQUE (workspace_id, aggregate_id, aggregate_sequence)"
        in normalized_sql
    )
    assert "octet_length(event_json) <= 4194304" in normalized_sql
    assert "octet_length(result_json) <= 4194304" in normalized_sql
    assert "PRIMARY KEY (workspace_id, journal_sequence)" in normalized_sql
    assert "BIGSERIAL" not in sql


def test_journal_schema_protects_monotonic_immutable_facts(monkeypatch) -> None:
    sql = render_upgrade_sql(monkeypatch)

    assert "CREATE TRIGGER harness_journal_events_immutable" in sql
    assert "CREATE TRIGGER harness_journal_idempotency_immutable" in sql
    assert "CREATE TRIGGER harness_journal_aggregates_monotonic" in sql
    assert "CREATE TRIGGER harness_journal_positions_monotonic" in sql
    assert "NEW.current_sequence < OLD.current_sequence" in sql
