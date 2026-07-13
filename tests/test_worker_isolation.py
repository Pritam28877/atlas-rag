from pathlib import Path
from uuid import UUID

import pytest

from app.workers.runtime import job_temp_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_COMPOSE = PROJECT_ROOT / "docker" / "compose.workers.yml"
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
    dockerfile = WORKER_DOCKERFILE.read_text(encoding="utf-8")

    assert "target: native" in compose
    assert "target: ocr" in compose
    assert compose.count("read_only: true") == 2
    assert compose.count("/var/lib/rag-jobs:rw,noexec,nosuid") == 2
    assert compose.count("uid=10001,gid=10001") == 2
    assert "internal: true" in compose
    assert "USER ragworker:ragworker" in dockerfile
    assert "tesseract-ocr=5.3.4-1build5" in dockerfile
