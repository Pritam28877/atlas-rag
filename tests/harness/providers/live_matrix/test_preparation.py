import asyncio
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from app.cli.harness.live_matrix_io import build_live_matrix_from_manifest
from app.cli.harness.live_matrix_preparation import (
    LiveMatrixPreparationRequest,
    prepare_live_matrix_manifest,
)
from app.services.harness.providers.live_matrix_descriptors import (
    LiveProviderTarget,
)
from tests.harness.providers.live_matrix.descriptor_fixtures import (
    conformance_report,
    provider_targets,
)
from tests.harness.providers.live_matrix.fixtures import (
    REQUIRED_PROVIDERS,
)


def test_preparation_derives_manifest_that_builds_private_matrix(
    tmp_path: Path,
) -> None:
    paths = _private_inputs(tmp_path)

    manifest = asyncio.run(
        prepare_live_matrix_manifest(*paths[:3])
    )
    report = conformance_report()
    matrix = asyncio.run(
        build_live_matrix_from_manifest(
            paths[2],
            clock=lambda: report.observed_at + timedelta(seconds=2),
        )
    )

    persisted = paths[2].read_text()
    assert len(manifest.providers) == 6
    assert tuple(
        descriptor.adapter_revision_sha256
        for descriptor in manifest.providers
    ) == tuple(
        adapter.adapter_revision_sha256
        for adapter in report.adapters
    )
    assert stat.S_IMODE(paths[2].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths[3].stat().st_mode) == 0o600
    assert '"observations"' not in persisted
    assert len(matrix.observations) == 48
    assert not matrix.release_ready


def test_preparation_rejects_mismatched_targets_without_output(
    tmp_path: Path,
) -> None:
    paths = _private_inputs(
        tmp_path,
        targets=provider_targets()[:-1],
    )

    with pytest.raises(ValueError, match="match"):
        asyncio.run(prepare_live_matrix_manifest(*paths[:3]))

    assert not paths[2].exists()


def test_preparation_rejects_path_collision_without_output(
    tmp_path: Path,
) -> None:
    paths = _private_inputs(tmp_path)

    with pytest.raises(ValueError, match="collide"):
        asyncio.run(
            prepare_live_matrix_manifest(
                paths[0],
                paths[1],
                paths[3],
            )
        )

    assert not paths[3].exists()


@pytest.mark.parametrize("input_index", (0, 1))
def test_preparation_rejects_permissive_input_without_output(
    tmp_path: Path,
    input_index: int,
) -> None:
    paths = _private_inputs(tmp_path)
    paths[input_index].chmod(0o644)

    with pytest.raises(ValueError, match="owner-only"):
        asyncio.run(prepare_live_matrix_manifest(*paths[:3]))

    assert not paths[2].exists()


def _private_inputs(
    tmp_path: Path,
    *,
    targets: tuple[LiveProviderTarget, ...] | None = None,
) -> tuple[Path, Path, Path, Path]:
    tmp_path.chmod(0o700)
    report_path = tmp_path / "conformance.json"
    preparation_path = tmp_path / "preparation.json"
    manifest_path = tmp_path / "manifest.json"
    result_path = tmp_path / "matrix.json"
    report_path.write_text(conformance_report().model_dump_json())
    selected_targets = (
        provider_targets() if targets is None else targets
    )
    preparation = LiveMatrixPreparationRequest(
        targets=selected_targets,
        evidence=(),
        required_live_providers=REQUIRED_PROVIDERS,
        authorized_cost_cap_microusd=20,
        result_path=result_path,
    )
    preparation_path.write_text(preparation.model_dump_json())
    report_path.chmod(0o600)
    preparation_path.chmod(0o600)
    return report_path, preparation_path, manifest_path, result_path
