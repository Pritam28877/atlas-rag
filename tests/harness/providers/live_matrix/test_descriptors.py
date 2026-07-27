import asyncio
from datetime import timedelta

import pytest

from app.services.harness.providers.conformance_contracts import (
    ConformanceReport,
)
from app.services.harness.providers.conformance_runner import (
    BoundedConformanceRunner,
)
from app.services.harness.providers.live_matrix_descriptors import (
    LiveProviderTarget,
    derive_live_provider_descriptors,
)
from tests.harness.providers.conformance.fixtures import NOW
from tests.harness.providers.conformance.mock_provider import (
    mock_conformance_adapter,
)
from tests.harness.providers.conformance.native_cloud import (
    native_cloud_adapters,
)
from tests.harness.providers.conformance.openai_family import (
    openai_family_adapters,
)
from tests.harness.providers.live_matrix.fixtures import descriptors


def test_descriptors_use_verified_adapter_revisions_and_scenarios() -> None:
    report = _report()
    derived = derive_live_provider_descriptors(report, _targets())

    assert tuple(item.provider for item in derived) == tuple(
        adapter.provider for adapter in report.adapters
    )
    assert tuple(
        item.adapter_revision_sha256 for item in derived
    ) == tuple(
        adapter.adapter_revision_sha256 for adapter in report.adapters
    )
    assert tuple(
        item.supported_scenarios for item in derived
    ) == tuple(
        adapter.supported_scenarios for adapter in report.adapters
    )
    assert all(
        item.observed_at == NOW + timedelta(seconds=1)
        for item in derived
    )


def test_target_set_must_exactly_match_conformance_report() -> None:
    report = _report()

    with pytest.raises(ValueError, match="match"):
        derive_live_provider_descriptors(report, _targets()[:-1])
    with pytest.raises(ValueError, match="match"):
        derive_live_provider_descriptors(
            report,
            (*_targets()[:-1], _targets()[0]),
        )


def _report() -> ConformanceReport:
    adapters = (
        mock_conformance_adapter(),
        *native_cloud_adapters(),
        *openai_family_adapters(),
    )
    return asyncio.run(
        BoundedConformanceRunner(clock=lambda: NOW).run(
            adapters,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )


def _targets() -> tuple[LiveProviderTarget, ...]:
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
