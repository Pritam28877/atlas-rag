import asyncio
from datetime import timedelta

from app.services.harness.providers import (
    CONFORMANCE_SCENARIOS,
    ConformanceCaseStatus,
    ConformanceComparisonStatus,
)
from app.services.harness.providers.conformance_runner import (
    BoundedConformanceRunner,
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


def test_all_six_adapters_pass_one_scenario_vocabulary() -> None:
    adapters = (
        mock_conformance_adapter(),
        *native_cloud_adapters(),
        *openai_family_adapters(),
    )
    report = asyncio.run(
        BoundedConformanceRunner(clock=lambda: NOW).run(
            adapters,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )

    assert tuple(
        adapter.provider for adapter in report.adapters
    ) == (
        "bedrock",
        "local-compatible",
        "mock",
        "openai",
        "openrouter",
        "vertex",
    )
    assert report.scenarios == CONFORMANCE_SCENARIOS
    assert len(report.observations) == 6 * len(
        CONFORMANCE_SCENARIOS
    )
    assert all(
        observation.status is ConformanceCaseStatus.PASSED
        for observation in report.observations
    )
    assert all(
        comparison.status
        is ConformanceComparisonStatus.EQUIVALENT
        and len(comparison.eligible_providers) == 6
        for comparison in report.comparisons
    )
