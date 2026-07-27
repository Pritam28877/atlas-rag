import asyncio
from datetime import timedelta

from app.services.harness.protocol import (
    ProviderFailureClass,
    ProviderFinishReason,
)
from app.services.harness.providers import (
    CONFORMANCE_SCENARIOS,
    ConformanceCaseStatus,
    ConformanceComparisonStatus,
    ConformanceScenario,
)
from app.services.harness.providers.conformance_runner import (
    BoundedConformanceRunner,
)
from app.services.harness.providers.conformance_suite import (
    openai_family_conformance_adapters,
)
from tests.harness.providers.conformance.fixtures import NOW


def test_openai_family_passes_the_complete_scenario_vocabulary() -> None:
    adapters = openai_family_conformance_adapters(
        clock=lambda: NOW,
    )
    report = asyncio.run(
        BoundedConformanceRunner(clock=lambda: NOW).run(
            adapters,
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=30),
        )
    )

    assert report.scenarios == CONFORMANCE_SCENARIOS
    assert tuple(
        adapter.provider for adapter in report.adapters
    ) == ("local-compatible", "openai", "openrouter")
    assert len(
        {
            adapter.adapter_revision_sha256
            for adapter in report.adapters
        }
    ) == 3
    assert all(
        observation.status is ConformanceCaseStatus.PASSED
        for observation in report.observations
    )
    assert all(
        comparison.status
        is ConformanceComparisonStatus.EQUIVALENT
        for comparison in report.comparisons
    )
    _assert_scenario_evidence(report)


def _assert_scenario_evidence(report) -> None:
    by_scenario = {
        observation.scenario: observation
        for observation in report.observations
        if observation.provider == "openai"
    }
    malformed = by_scenario[ConformanceScenario.MALFORMED_STREAM]
    assert malformed.outcome is not None
    assert (
        malformed.outcome.failure_class
        is ProviderFailureClass.MALFORMED
    )
    policy = by_scenario[ConformanceScenario.POLICY]
    assert policy.outcome is not None
    assert policy.outcome.failure_class is ProviderFailureClass.POLICY
    limited = by_scenario[ConformanceScenario.OUTPUT_LIMIT]
    assert limited.outcome is not None
    assert limited.outcome.finish_reason is ProviderFinishReason.LENGTH
    assert (
        "redacted fixture detail" not in report.model_dump_json()
    )
