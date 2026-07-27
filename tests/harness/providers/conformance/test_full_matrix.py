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
from app.services.harness.providers.conformance_suite import (
    recorded_conformance_adapters,
)
from tests.harness.providers.conformance.fixtures import NOW


def test_all_six_adapters_pass_one_scenario_vocabulary() -> None:
    adapters = recorded_conformance_adapters(
        clock=lambda: NOW,
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
