import asyncio
from datetime import timedelta

from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
)
from app.services.harness.providers.local_compatible_probe import (
    BoundedLocalCompatibleProbeRunner,
)
from app.services.harness.providers.local_compatible_probe_backend import (
    LocalCompatibleProbeBackend,
)
from tests.harness.providers.local_compatible.fixtures import (
    NOW,
    authorized_route,
    local_model,
)
from tests.harness.providers.local_compatible.probe.fixtures import (
    QueuedConnector,
    compatible_streams,
)


def test_six_cases_produce_bound_capability_evidence() -> None:
    connector = QueuedConnector(compatible_streams())
    route = authorized_route()
    backend = LocalCompatibleProbeBackend(
        connector,
        route,
        None,
        clock=lambda: NOW,
    )
    runner = BoundedLocalCompatibleProbeRunner(
        backend,
        clock=lambda: NOW,
    )

    probe = asyncio.run(
        runner.probe(
            route,
            model_revision_sha256=local_model().model_revision_sha256,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )

    assert set(probe.supported_features) == set(LocalCompatibleFeature)
    assert probe.destination_sha256 == route.destination_sha256
    assert connector.request_count == 6
    assert connector.closed_streams == 6
    assert "ready" not in probe.model_dump_json()
