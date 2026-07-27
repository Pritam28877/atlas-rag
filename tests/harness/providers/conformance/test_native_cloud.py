import asyncio
from datetime import timedelta

from app.services.harness.providers import (
    ConformanceCaseStatus,
    ConformanceComparisonStatus,
)
from app.services.harness.providers.conformance_runner import (
    BoundedConformanceRunner,
)
from app.services.harness.providers.conformance_suite import (
    native_cloud_conformance_adapters,
    openai_family_conformance_adapters,
)
from tests.harness.providers.conformance.fixtures import NOW


def test_native_cloud_and_openai_family_are_contract_equivalent() -> None:
    adapters = (
        *native_cloud_conformance_adapters(clock=lambda: NOW),
        *openai_family_conformance_adapters(clock=lambda: NOW),
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
        "openai",
        "openrouter",
        "vertex",
    )
    assert all(
        observation.status is ConformanceCaseStatus.PASSED
        for observation in report.observations
    )
    assert all(
        comparison.status
        is ConformanceComparisonStatus.EQUIVALENT
        for comparison in report.comparisons
    )
    serialized = report.model_dump_json()
    assert "bedrock_call" not in serialized
    assert "HARM_CATEGORY" not in serialized
