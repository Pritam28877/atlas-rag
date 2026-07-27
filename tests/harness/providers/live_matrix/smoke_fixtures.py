"""Redacted smoke results shared by live-matrix tests."""

from datetime import timedelta

from app.cli.harness.adapter_smoke_io import AdapterSmokeResult
from app.cli.harness.bedrock_smoke_io import BedrockSmokeResult
from app.cli.harness.live_matrix_failure import LiveSmokeFailureReceipt
from app.cli.harness.provider_smoke_io import ProviderSmokeResult
from app.services.harness.providers.conformance_contracts import (
    ConformanceScenario,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveEvidenceFailureCode,
)
from tests.harness.providers.live_matrix.fixtures import NOW


def adapter_result() -> AdapterSmokeResult:
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


def provider_result() -> ProviderSmokeResult:
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


def bedrock_result() -> BedrockSmokeResult:
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


def failure_receipt() -> LiveSmokeFailureReceipt:
    return LiveSmokeFailureReceipt(
        provider="vertex",
        model="configured-model",
        model_revision_sha256="6" * 64,
        adapter_revision_sha256="a" * 64,
        route_binding_sha256="b" * 64,
        scenarios=(
            ConformanceScenario.TEXT_STREAM,
            ConformanceScenario.USAGE_COST,
        ),
        failure_code=LiveEvidenceFailureCode.TIMEOUT,
        latency_ms=30_000,
        charged_cost_microusd=4,
        authorized_cost_cap_microusd=10,
        trace_sha256="d" * 64,
        occurred_at=NOW + timedelta(seconds=1),
    )
