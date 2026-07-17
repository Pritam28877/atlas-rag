import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.readiness import (
    DependencyStatus,
    InfrastructureReadiness,
    ReadinessReport,
)
from app.main import app


async def ready() -> None:
    return None


async def unavailable() -> None:
    raise RuntimeError("dependency failure")


def test_readiness_reports_safe_unavailable_status() -> None:
    readiness = InfrastructureReadiness(
        Settings(_env_file=None),
        checks={"database": ready, "storage": unavailable},
    )

    report = asyncio.run(readiness.check())

    assert report.ready is False
    assert report.dependencies == {
        "database": DependencyStatus.READY,
        "storage": DependencyStatus.UNAVAILABLE,
    }
    assert "dependency failure" not in str(report.as_dict())


class StaticReadiness:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            ready=True,
            dependencies={
                "database": DependencyStatus.READY,
                "storage": DependencyStatus.NOT_CONFIGURED,
                "broker": DependencyStatus.NOT_CONFIGURED,
                "workers": DependencyStatus.NOT_CONFIGURED,
                "scheduler": DependencyStatus.NOT_CONFIGURED,
                "search": DependencyStatus.NOT_CONFIGURED,
            },
        )


def test_readiness_endpoint_reports_safe_dependency_state() -> None:
    with TestClient(app) as client:
        app.state.readiness = StaticReadiness()
        response = client.get("/v1/ready")

    assert response.status_code == 200
    assert response.json()["dependencies"] == {
        "database": "ready",
        "storage": "not_configured",
        "broker": "not_configured",
        "workers": "not_configured",
        "scheduler": "not_configured",
        "search": "not_configured",
    }


def test_readiness_checks_are_coalesced_and_cached() -> None:
    calls = 0

    async def counted_check() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)

    readiness = InfrastructureReadiness(
        Settings(_env_file=None),
        checks={"database": counted_check},
    )

    async def check_concurrently() -> None:
        reports = await asyncio.gather(
            readiness.check(),
            readiness.check(),
            readiness.check(),
        )
        assert reports[0] is reports[1] is reports[2]
        assert await readiness.check() is reports[0]

    asyncio.run(check_concurrently())
    assert calls == 1


class _SchedulerSession:
    def __init__(self, fresh: bool) -> None:
        self.fresh = fresh
        self.parameters: dict[str, object] | None = None

    async def scalar(self, _statement, parameters):
        self.parameters = parameters
        return self.fresh


class _SchedulerDatabase:
    instances: list["_SchedulerDatabase"] = []
    fresh = True

    def __init__(self, _settings) -> None:
        self.session = _SchedulerSession(self.fresh)
        self.closed = False
        self.instances.append(self)

    @asynccontextmanager
    async def transaction(self):
        yield self.session

    async def close(self) -> None:
        self.closed = True


def test_scheduler_readiness_uses_fresh_heartbeat_and_closes_database() -> None:
    settings = Settings(
        _env_file=None,
        database={"url": "postgresql://test:test@localhost:5432/test"},
        broker={"url": "amqp://test:test@localhost:5672/test", "use_tls": False},
        readiness={"scheduler_heartbeat_max_age_seconds": 120},
    )
    readiness = InfrastructureReadiness(settings)
    _SchedulerDatabase.instances.clear()
    _SchedulerDatabase.fresh = True

    with patch("app.core.readiness.Database", _SchedulerDatabase):
        asyncio.run(readiness._check_scheduler())

    database = _SchedulerDatabase.instances[-1]
    assert database.session.parameters == {"maximum_age": 120}
    assert database.closed is True


def test_scheduler_readiness_rejects_stale_heartbeat() -> None:
    settings = Settings(
        _env_file=None,
        database={"url": "postgresql://test:test@localhost:5432/test"},
        broker={"url": "amqp://test:test@localhost:5672/test", "use_tls": False},
    )
    readiness = InfrastructureReadiness(settings)
    _SchedulerDatabase.instances.clear()
    _SchedulerDatabase.fresh = False

    with patch("app.core.readiness.Database", _SchedulerDatabase):
        try:
            asyncio.run(readiness._check_scheduler())
        except RuntimeError as error:
            assert str(error) == "scheduler heartbeat is stale"
        else:
            raise AssertionError("stale scheduler heartbeat was accepted")

    assert _SchedulerDatabase.instances[-1].closed is True
