from datetime import timedelta

import pytest

from app.services.harness.providers.live_matrix_descriptors import (
    derive_live_provider_descriptors,
)
from tests.harness.providers.conformance.fixtures import NOW
from tests.harness.providers.live_matrix.descriptor_fixtures import (
    conformance_report,
    provider_targets,
)


def test_descriptors_use_verified_adapter_revisions_and_scenarios() -> None:
    report = conformance_report()
    derived = derive_live_provider_descriptors(
        report,
        provider_targets(),
    )

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
    report = conformance_report()
    targets = provider_targets()

    with pytest.raises(ValueError, match="match"):
        derive_live_provider_descriptors(report, targets[:-1])
    with pytest.raises(ValueError, match="match"):
        derive_live_provider_descriptors(
            report,
            (*targets[:-1], targets[0]),
        )
