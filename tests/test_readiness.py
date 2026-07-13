import asyncio

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.readiness import DependencyStatus, InfrastructureReadiness
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


def test_readiness_endpoint_reports_local_dependency_state() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/ready")

    assert response.status_code == 200
    assert response.json()["dependencies"] == {
        "database": "ready",
        "storage": "not_configured",
        "broker": "not_configured",
        "workers": "not_configured",
    }
