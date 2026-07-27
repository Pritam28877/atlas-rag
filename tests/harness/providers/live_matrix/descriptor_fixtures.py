"""Conformance-derived descriptor fixtures."""

import asyncio
from datetime import timedelta

from app.services.harness.providers.conformance_contracts import (
    ConformanceReport,
)
from app.services.harness.providers.conformance_runner import (
    BoundedConformanceRunner,
)
from app.services.harness.providers.conformance_suite import (
    recorded_conformance_adapters,
)
from app.services.harness.providers.live_matrix_descriptors import (
    LiveProviderTarget,
)
from tests.harness.providers.conformance.fixtures import NOW
from tests.harness.providers.live_matrix.fixtures import descriptors


def conformance_report() -> ConformanceReport:
    adapters = recorded_conformance_adapters(
        clock=lambda: NOW,
    )
    return asyncio.run(
        BoundedConformanceRunner(clock=lambda: NOW).run(
            adapters,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )


def provider_targets() -> tuple[LiveProviderTarget, ...]:
    return tuple(
        LiveProviderTarget(
            provider=descriptor.provider,
            environment=descriptor.environment,
            model=descriptor.model,
            model_revision_sha256=(
                descriptor.model_revision_sha256
            ),
            route_binding_sha256=(
                descriptor.route_binding_sha256
            ),
            observed_at=NOW + timedelta(seconds=1),
        )
        for descriptor in descriptors()
    )
