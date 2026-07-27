from datetime import timedelta

import pytest

from app.cli.harness.adapter_smoke_io import AdapterSmokeResult
from app.cli.harness.bedrock_smoke_io import BedrockSmokeResult
from app.cli.harness.live_matrix_smoke import (
    live_observations_from_smoke,
)
from app.cli.harness.provider_smoke_io import ProviderSmokeResult
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


def test_adapter_smoke_maps_text_and_cost_once() -> None:
    descriptor = descriptors()[1]
    observations = live_observations_from_smoke(
        descriptor,
        _adapter_result(),
    )

    assert tuple(item.scenario for item in observations) == (
        ConformanceScenario.TEXT_STREAM,
        ConformanceScenario.USAGE_COST,
    )
    assert observations[0].charged_cost_microusd is None
    assert observations[1].charged_cost_microusd == 0
    assert observations[1].usage is not None
    assert observations[0].trace_sha256 == observations[1].trace_sha256


def test_bedrock_smoke_maps_cancellation_tools_and_aggregate_usage() -> None:
    descriptor = descriptors()[0]
    observations = live_observations_from_smoke(
        descriptor,
        _bedrock_result(),
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
        _provider_result(),
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
    mismatched = _provider_result().model_copy(
        update={"route_binding_sha256": "f" * 64}
    )

    with pytest.raises(ValueError, match="binding"):
        live_observations_from_smoke(descriptor, mismatched)
    leaked = _provider_result().model_copy(
        update={"active_credential_leases": 1}
    )
    with pytest.raises(ValueError, match="cleanup"):
        live_observations_from_smoke(descriptor, leaked)


def _adapter_result() -> AdapterSmokeResult:
    return AdapterSmokeResult(
        provider="local-compatible",
        model="configured-model",
        request_id="req_" + "1" * 32,
        event_count=3,
        output_sha256="1" * 64,
        canonical_events_sha256="2" * 64,
        route_binding_sha256="b" * 64,
        input_tokens=2,
        cached_input_tokens=0,
        output_tokens=1,
        reasoning_tokens=0,
        latency_ms=5,
        charged_cost_microusd=0,
        completed_at=NOW + timedelta(seconds=1),
    )


def _provider_result() -> ProviderSmokeResult:
    return ProviderSmokeResult(
        provider="openai",
        model="configured-model",
        request_id="req_" + "2" * 32,
        route_binding_sha256="b" * 64,
        response_status=200,
        response_body_sha256="3" * 64,
        input_tokens=3,
        cached_input_tokens=0,
        output_tokens=2,
        reasoning_tokens=0,
        latency_ms=6,
        charged_cost_microusd=7,
        audit_events=2,
        active_credential_leases=0,
        completed_at=NOW + timedelta(seconds=1),
    )


def _bedrock_result() -> BedrockSmokeResult:
    return BedrockSmokeResult(
        model="configured-model",
        region="us-east-1",
        authorization_id_sha256="4" * 64,
        destination_sha256="5" * 64,
        route_binding_sha256="b" * 64,
        text_request_sha256="6" * 64,
        tool_request_sha256="7" * 64,
        input_tokens=4,
        cached_input_tokens=0,
        output_tokens=3,
        reasoning_tokens=0,
        latency_ms=8,
        cancellation_latency_ms=3,
        charged_cost_microusd=9,
        signed_cost_cap_microusd=10,
        completed_at=NOW + timedelta(seconds=1),
    )
