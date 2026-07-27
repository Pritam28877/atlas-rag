import asyncio
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from app.cli.harness.live_matrix_io import (
    LiveMatrixManifest,
    LiveSmokeEvidenceReference,
    LiveSmokeResultKind,
    build_live_matrix_from_manifest,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveConformanceMatrix,
)
from tests.harness.providers.live_matrix.fixtures import (
    NOW,
    REQUIRED_PROVIDERS,
    descriptors,
)
from tests.harness.providers.live_matrix.smoke_fixtures import (
    failure_receipt,
    provider_result,
)


def test_private_smoke_results_build_private_complete_matrix(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    smoke_path = tmp_path / "openai-result.json"
    matrix_path = tmp_path / "matrix.json"
    manifest_path = tmp_path / "manifest.json"
    _write_private(smoke_path, provider_result().model_dump_json())
    manifest = LiveMatrixManifest(
        providers=descriptors(),
        evidence=(
            LiveSmokeEvidenceReference(
                provider="openai",
                kind=LiveSmokeResultKind.PROVIDER,
                result_path=smoke_path,
            ),
        ),
        required_live_providers=REQUIRED_PROVIDERS,
        authorized_cost_cap_microusd=20,
        result_path=matrix_path,
    )
    _write_private(manifest_path, manifest.model_dump_json())

    matrix = asyncio.run(
        build_live_matrix_from_manifest(
            manifest_path,
            clock=lambda: NOW + timedelta(seconds=2),
        )
    )

    persisted = LiveConformanceMatrix.model_validate_json(
        matrix_path.read_bytes()
    )
    assert persisted == matrix
    assert matrix.charged_cost_microusd == 7
    assert not matrix.release_ready
    assert stat.S_IMODE(matrix_path.stat().st_mode) == 0o600
    assert "req_" not in matrix_path.read_text()


def test_permissive_smoke_result_is_rejected_without_output(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    smoke_path = tmp_path / "openai-result.json"
    matrix_path = tmp_path / "matrix.json"
    manifest_path = tmp_path / "manifest.json"
    _write_private(smoke_path, provider_result().model_dump_json())
    smoke_path.chmod(0o644)
    manifest = LiveMatrixManifest(
        providers=descriptors(),
        evidence=(
            LiveSmokeEvidenceReference(
                provider="openai",
                kind=LiveSmokeResultKind.PROVIDER,
                result_path=smoke_path,
            ),
        ),
        required_live_providers=REQUIRED_PROVIDERS,
        authorized_cost_cap_microusd=20,
        result_path=matrix_path,
    )
    _write_private(manifest_path, manifest.model_dump_json())

    with pytest.raises(ValueError, match="owner-only"):
        asyncio.run(build_live_matrix_from_manifest(manifest_path))

    assert not matrix_path.exists()


def test_private_failure_receipt_populates_failed_matrix_cells(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    failure_path = tmp_path / "vertex-failure.json"
    matrix_path = tmp_path / "matrix.json"
    manifest_path = tmp_path / "manifest.json"
    _write_private(failure_path, failure_receipt().model_dump_json())
    manifest = LiveMatrixManifest(
        providers=descriptors(),
        evidence=(
            LiveSmokeEvidenceReference(
                provider="vertex",
                kind=LiveSmokeResultKind.FAILURE,
                result_path=failure_path,
            ),
        ),
        required_live_providers=REQUIRED_PROVIDERS,
        authorized_cost_cap_microusd=20,
        result_path=matrix_path,
    )
    _write_private(manifest_path, manifest.model_dump_json())

    matrix = asyncio.run(
        build_live_matrix_from_manifest(
            manifest_path,
            clock=lambda: NOW + timedelta(seconds=2),
        )
    )

    failed = tuple(
        observation
        for observation in matrix.observations
        if observation.provider == "vertex"
        and observation.status.value == "failed"
    )
    assert len(failed) == 2
    assert matrix.charged_cost_microusd == 4


def _write_private(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o600)
