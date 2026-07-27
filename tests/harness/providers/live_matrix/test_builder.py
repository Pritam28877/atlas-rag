from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.providers.conformance_contracts import (
    CONFORMANCE_SCENARIOS,
    ConformanceScenario,
)
from app.services.harness.providers.live_matrix_builder import (
    build_live_conformance_matrix,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveEvidenceFailureCode,
    LiveEvidenceObservation,
    LiveEvidenceStatus,
)
from tests.harness.providers.live_matrix.fixtures import (
    NOW,
    REQUIRED_PROVIDERS,
    descriptors,
    passed,
)


def test_missing_runs_and_capabilities_are_explicit() -> None:
    provider_descriptors = list(descriptors())
    vertex = provider_descriptors[-1]
    provider_descriptors[-1] = vertex.model_copy(
        update={
            "supported_scenarios": tuple(
                scenario
                for scenario in CONFORMANCE_SCENARIOS
                if scenario is not ConformanceScenario.REASONING_STREAM
            )
        }
    )

    matrix = build_live_conformance_matrix(
        provider_descriptors,
        (),
        required_live_providers=REQUIRED_PROVIDERS,
        authorized_cost_cap_microusd=1_000,
        generated_at=NOW + timedelta(seconds=2),
    )

    assert len(matrix.observations) == 6 * len(CONFORMANCE_SCENARIOS)
    assert not matrix.release_ready
    assert matrix.charged_cost_microusd == 0
    assert {
        observation.status for observation in matrix.observations
    } == {
        LiveEvidenceStatus.UNSUPPORTED,
        LiveEvidenceStatus.UNTESTED,
    }


def test_required_live_scenarios_make_release_evidence_complete() -> None:
    observations = tuple(
        passed(
            provider,
            scenario,
            cost_microusd=5,
        )
        for provider in REQUIRED_PROVIDERS
        for scenario in (
            ConformanceScenario.CANCELLATION,
            ConformanceScenario.TEXT_STREAM,
            ConformanceScenario.USAGE_COST,
        )
    )

    matrix = build_live_conformance_matrix(
        descriptors(),
        observations,
        required_live_providers=REQUIRED_PROVIDERS,
        authorized_cost_cap_microusd=20,
        generated_at=NOW + timedelta(seconds=2),
    )

    assert matrix.release_ready
    assert matrix.charged_cost_microusd == 15


def test_failed_evidence_is_distinct_and_redacted_to_trace_hash() -> None:
    descriptor = descriptors()[0]
    failed = LiveEvidenceObservation(
        provider=descriptor.provider,
        model_revision_sha256=descriptor.model_revision_sha256,
        adapter_revision_sha256=descriptor.adapter_revision_sha256,
        route_binding_sha256=descriptor.route_binding_sha256,
        scenario=ConformanceScenario.TEXT_STREAM,
        status=LiveEvidenceStatus.FAILED,
        latency_ms=20,
        trace_sha256="d" * 64,
        failure_code=LiveEvidenceFailureCode.TIMEOUT,
        reason="Live scenario timed out.",
        observed_at=NOW + timedelta(seconds=1),
    )

    matrix = build_live_conformance_matrix(
        descriptors(),
        (failed,),
        required_live_providers=REQUIRED_PROVIDERS,
        authorized_cost_cap_microusd=20,
        generated_at=NOW + timedelta(seconds=2),
    )

    recorded = next(
        observation
        for observation in matrix.observations
        if observation.provider == "bedrock"
        and observation.scenario is ConformanceScenario.TEXT_STREAM
    )
    assert recorded.status is LiveEvidenceStatus.FAILED
    assert recorded.failure_code is LiveEvidenceFailureCode.TIMEOUT
    assert "trace_sha256" in recorded.model_dump()
    assert "trace" not in recorded.model_dump()


def test_cost_cap_and_incomplete_status_evidence_fail_closed() -> None:
    observations = (
        passed(
            "openai",
            ConformanceScenario.USAGE_COST,
            cost_microusd=21,
        ),
    )
    with pytest.raises(ValidationError, match="cost evidence"):
        build_live_conformance_matrix(
            descriptors(),
            observations,
            required_live_providers=REQUIRED_PROVIDERS,
            authorized_cost_cap_microusd=20,
            generated_at=NOW + timedelta(seconds=2),
        )
    with pytest.raises(ValidationError, match="runtime measurements"):
        LiveEvidenceObservation(
            provider="openai",
            model_revision_sha256="4" * 64,
            adapter_revision_sha256="a" * 64,
            route_binding_sha256="b" * 64,
            scenario=ConformanceScenario.TEXT_STREAM,
            status=LiveEvidenceStatus.UNTESTED,
            latency_ms=1,
            reason="Not run.",
            observed_at=NOW,
        )
