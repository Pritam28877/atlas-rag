import os
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.auth import Principal, require_principal
from app.core.config import get_settings
from app.core.database import normalize_database_url
from app.main import create_app
from app.workers.celery_app import check_worker_connection
from tests.document_loader_e2e_support import (
    configure_environment,
    delete_index,
    delete_tenant,
    request_lifecycle,
    required_environment,
    search_hit_count,
    submit_pdf,
    wait_for_search,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCANNED_PDF = PROJECT_ROOT / "benchmarks" / "fixtures" / "scan-en-001.pdf"
TERMINAL_FAILURE_STATES = {
    "cancelled",
    "deduplicated",
    "failed",
    "quarantined",
    "rejected",
}


@pytest.mark.worker_integration
def test_scanned_pdf_runs_through_isolated_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = required_environment(require_broker=True, require_model=False)
    index_name = os.environ.get("P2_WORKER_SEARCH_INDEX_NAME")
    if not index_name:
        pytest.skip("P2_WORKER_SEARCH_INDEX_NAME is not configured")
    configure_environment(monkeypatch, values, index_name, tmp_path)
    settings = get_settings()
    wait_for_search(settings)
    check_worker_connection(settings)
    command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), "head")

    engine = create_engine(
        normalize_database_url(values["TEST_DATABASE_URL"]),
        pool_pre_ping=True,
    )
    tenant_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": f"worker-e2e-{tenant_id}"},
        )

    try:
        version_id, _, _ = submit_pdf(
            tenant_id,
            SCANNED_PDF,
            name=f"worker-e2e-{uuid4().hex}",
        )
        _wait_for_state(tenant_id, version_id, "ready")
        _assert_worker_evidence(engine, tenant_id, version_id)
        assert search_hit_count(settings, tenant_id, version_id) > 0

        deleted_id = request_lifecycle(
            tenant_id,
            version_id,
            "delete",
            f"worker-delete-{uuid4().hex}",
        )
        _wait_for_state(tenant_id, deleted_id, "deleted")
        assert search_hit_count(settings, tenant_id, deleted_id) == 0
        with engine.connect() as connection:
            remaining_artifacts = connection.scalar(
                text(
                    """
                    SELECT count(*) FROM artifacts
                    WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                    """
                ),
                {"tenant_id": tenant_id, "version_id": deleted_id},
            )
        assert remaining_artifacts == 0
    finally:
        delete_index(settings)
        delete_tenant(engine, tenant_id)
        engine.dispose()
        get_settings.cache_clear()


def _wait_for_state(
    tenant_id: UUID,
    version_id: UUID,
    expected_state: str,
    timeout_seconds: float = 180,
) -> None:
    application = create_app()
    application.dependency_overrides[require_principal] = lambda: Principal(
        subject="document-loader-e2e",
        tenant_id=tenant_id,
    )
    deadline = time.monotonic() + timeout_seconds
    with TestClient(application) as client:
        while time.monotonic() < deadline:
            response = client.get(f"/v1/document-versions/{version_id}")
            assert response.status_code == 200, response.text
            state = str(response.json()["state"])
            if state == expected_state:
                return
            if state in TERMINAL_FAILURE_STATES:
                pytest.fail(f"worker pipeline terminated in {state}")
            time.sleep(0.25)
    pytest.fail(f"worker pipeline did not reach {expected_state}")


def _assert_worker_evidence(
    engine: Engine,
    tenant_id: UUID,
    version_id: UUID,
) -> None:
    with engine.connect() as connection:
        artifact_types = set(
            connection.scalars(
                text(
                    """
                    SELECT artifact_type FROM artifacts
                    WHERE tenant_id = :tenant_id
                      AND document_version_id = :version_id
                    """
                ),
                {"tenant_id": tenant_id, "version_id": version_id},
            )
        )
        job_rows = connection.execute(
            text(
                """
                SELECT stage, state FROM ingestion_jobs
                WHERE tenant_id = :tenant_id
                  AND document_version_id = :version_id
                """
            ),
            {"tenant_id": tenant_id, "version_id": version_id},
        )
        job_states = {str(row.stage): str(row.state) for row in job_rows}
    assert "ocr_result" in artifact_types
    assert job_states == {
        "preflight": "succeeded",
        "ocr": "succeeded",
        "chunk": "succeeded",
        "embed": "succeeded",
        "index": "succeeded",
    }
