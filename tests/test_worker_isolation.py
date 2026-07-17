import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from app.workers.runtime import job_temp_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_COMPOSE = PROJECT_ROOT / "docker" / "compose.workers.yml"
LOCAL_PLATFORM_COMPOSE = PROJECT_ROOT / "docker" / "compose.local-platform.yml"
WORKER_DOCKERFILE = PROJECT_ROOT / "docker" / "worker" / "Dockerfile"


def test_job_temporary_directory_is_removed_after_success(tmp_path: Path) -> None:
    job_id = UUID("e4f1e42d-cc5f-48d7-b476-46743e94b5dc")

    with job_temp_directory(tmp_path, job_id) as temporary_directory:
        payload_path = temporary_directory / "document.pdf"
        payload_path.write_bytes(b"temporary document content")
        assert payload_path.exists()

    assert list(tmp_path.iterdir()) == []


def test_job_temporary_directory_is_removed_after_failure(tmp_path: Path) -> None:
    job_id = UUID("e4f1e42d-cc5f-48d7-b476-46743e94b5dc")

    with pytest.raises(RuntimeError, match="worker failure"):
        with job_temp_directory(tmp_path, job_id) as temporary_directory:
            (temporary_directory / "document.pdf").write_bytes(b"temporary content")
            raise RuntimeError("worker failure")

    assert list(tmp_path.iterdir()) == []


def test_worker_profiles_are_non_root_read_only_and_internal_only() -> None:
    compose = WORKER_COMPOSE.read_text(encoding="utf-8")
    local_platform = LOCAL_PLATFORM_COMPOSE.read_text(encoding="utf-8")
    dockerfile = WORKER_DOCKERFILE.read_text(encoding="utf-8")

    assert "target: native" in compose
    assert "target: ocr" in compose
    assert "target: publication" in compose
    assert "target: lifecycle" in compose
    assert "target: scheduler" in compose
    assert compose.count("read_only: true") == 5
    assert compose.count("/tmp:rw,noexec,nosuid,size=16m,mode=1777") == 5
    assert compose.count("/var/lib/rag-jobs:rw,noexec,nosuid") == 4
    assert compose.count("uid=10001,gid=10001") == 4
    assert "external: true" in compose
    assert "internal: true" in local_platform
    assert "host-access" in local_platform
    assert "atlas-rag-ingestion" in compose
    assert "atlas-rag-ingestion" in local_platform
    assert compose.count("RUNTIME_ROLE:") == 5
    assert "BROKER__USE_TLS" in compose
    assert "STORAGE__SERVER_SIDE_ENCRYPTION" in compose
    assert "SEARCH__VERIFY_TLS" in compose
    assert "USER ragworker:ragworker" in dockerfile
    assert 'CMD ["python", "-m", "app.workers.native_worker"]' in dockerfile
    assert 'CMD ["python", "-m", "app.workers.ocr_worker"]' in dockerfile
    assert 'CMD ["python", "-m", "app.workers.publication_worker"]' in dockerfile
    assert 'CMD ["python", "-m", "app.workers.lifecycle_worker"]' in dockerfile
    assert "tesseract-ocr=5.3.4-1build5" in dockerfile
    assert "poppler-utils=24.02.0-1ubuntu9.9" in dockerfile
    assert "faf4aa4225822f3bc6376869cb1164e8e3feedd0" in dockerfile
    assert "sha256sum --check --strict" in dockerfile


def test_rendered_worker_profiles_inherit_security_and_contract_settings() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is not installed")
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(PROJECT_ROOT / ".env.example"),
            "-f",
            str(WORKER_COMPOSE),
            "config",
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(completed.stdout)["services"]
    required_services = {
        "native-worker",
        "ocr-worker",
        "publication-worker",
        "lifecycle-worker",
        "reconciliation-scheduler",
    }
    assert set(services) == required_services
    for service in services.values():
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["init"] is True
        assert service["restart"] == "unless-stopped"
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        environment = service["environment"]
        assert environment["BROKER__QUEUE_MAX_MESSAGES"] == "10000"
        assert environment["WORKERS__JOB_MAX_ATTEMPTS"] == "3"
        assert environment["LIFECYCLE__RECONCILE_PAGE_SIZE"] == "200"
        assert environment["PROVIDER_TIMEOUTS__REQUEST_SECONDS"] == "30"

    publication = services["publication-worker"]["environment"]
    assert publication["DATABASE__POOL_MAX_SIZE"] == "10"
    assert publication["INGESTION__MAX_CHUNKS"] == "10000"
    assert publication["EMBEDDING__DIMENSIONS"] == "384"
    assert publication["SEARCH__TARGET_VERSION"] == "opensearch-2.19.5-hybrid-v1"
