from datetime import timedelta

import pytest

from app.cli.harness.live_matrix_smoke import (
    live_observations_from_smoke,
)
from app.services.harness.providers.conformance_contracts import (
    ConformanceScenario,
)
from app.services.harness.providers.live_matrix_builder import (
    build_live_conformance_matrix,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveEvidenceStatus,
)
from tests.harness.providers.live_matrix.fixtures import (
    NOW,
    REQUIRED_PROVIDERS,
    descriptors,
)
from tests.harness.providers.live_matrix.smoke_fixtures import (
    adapter_result,
    bedrock_result,
    provider_result,
)


def test_adapter_smoke_maps_text_and_cost_once() -> None:
    descriptor = descriptors()[1]
    observations = live_observations_from_smoke(
        descriptor,
        adapter_result(),
    )

    assert tuple(item.scenario for item in observations) == (
        ConformanceScenario.CANCELLATION,
        ConformanceScenario.TEXT_STREAM,
        ConformanceScenario.USAGE_COST,
    )
    assert observations[0].cancellation_latency_ms == 2
    assert observations[1].charged_cost_microusd is None
    assert observations[2].charged_cost_microusd == 0
    assert observations[2].usage is not None
    assert observations[0].trace_sha256 == observations[2].trace_sha256


def test_bedrock_smoke_maps_cancellation_tools_and_aggregate_usage() -> None:
    descriptor = descriptors()[0]
    observations = live_observations_from_smoke(
        descriptor,
        bedrock_result(),
    )

    assert tuple(item.scenario for item in observations) == (
        ConformanceScenario.CANCELLATION,
        ConformanceScenario.TEXT_STREAM,
        ConformanceScenario.TOOL_CALLS,
        ConformanceScenario.USAGE_COST,
    )
    assert observations[0].cancellation_latency_ms == 3
    assert observations[0].latency_ms == 3
    assert sum(item.charged_cost_microusd or 0 for item in observations) == 9


def test_provider_smoke_populates_matrix_without_erasing_untested_cells() -> None:
    provider_descriptors = descriptors()
    openai = provider_descriptors[3]
    observations = live_observations_from_smoke(
        openai,
        provider_result(),
    )

    matrix = build_live_conformance_matrix(
        provider_descriptors,
        observations,
        required_live_providers=REQUIRED_PROVIDERS,
        authorized_cost_cap_microusd=20,
        generated_at=NOW + timedelta(seconds=2),
    )

    assert not matrix.release_ready
    assert matrix.charged_cost_microusd == 7
    assert {
        item.status
        for item in matrix.observations
        if item.provider == "openai"
    } == {
        LiveEvidenceStatus.PASSED,
        LiveEvidenceStatus.UNTESTED,
    }


def test_smoke_descriptor_mismatch_fails_closed() -> None:
    descriptor = descriptors()[3]
    mismatched = provider_result().model_copy(
        update={"route_binding_sha256": "f" * 64}
    )

    with pytest.raises(ValueError, match="binding"):
        live_observations_from_smoke(descriptor, mismatched)
    leaked = provider_result().model_copy(
        update={"active_credential_leases": 1}
    )
    with pytest.raises(ValueError, match="cleanup"):
        live_observations_from_smoke(descriptor, leaked)
