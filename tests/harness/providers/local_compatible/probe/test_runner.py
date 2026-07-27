import asyncio
from pathlib import Path

import pytest

from app.cli.harness.local_probe_runner import (
    run_local_capability_probe,
)
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
)
from tests.harness.providers.local_compatible.fixtures import NOW
from tests.harness.providers.local_compatible.probe.fixtures import (
    QueuedConnector,
    authorized_probe,
    compatible_streams,
)


def test_probe_runs_six_calls_and_writes_redacted_private_evidence(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    authorized = authorized_probe(tmp_path)
    connector = QueuedConnector(compatible_streams())

    result = asyncio.run(
        run_local_capability_probe(
            authorized,
            connector=connector,
            environment={
                b"ATLAS_LOCAL_PROBE_ENABLED": b"enabled",
            },
            clock=lambda: NOW,
        )
    )

    assert set(result.supported_features) == set(LocalCompatibleFeature)
    assert connector.request_count == 6
    assert connector.closed_streams == 6
    persisted = authorized.result_path.read_text()
    assert "ready" not in persisted
    assert '"delta"' not in persisted
    assert authorized.result_path.stat().st_mode & 0o077 == 0


def test_closed_gate_has_no_file_or_provider_call(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    authorized = authorized_probe(tmp_path)
    connector = QueuedConnector(compatible_streams())

    with pytest.raises(ValueError, match="gate is closed"):
        asyncio.run(
            run_local_capability_probe(
                authorized,
                connector=connector,
                environment={},
                clock=lambda: NOW,
            )
        )

    assert connector.request_count == 0
    assert not authorized.result_path.exists()
