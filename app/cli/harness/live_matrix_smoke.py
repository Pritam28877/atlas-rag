"""Map redacted provider smoke results into live-matrix observations."""

from __future__ import annotations

import hashlib
import json

from app.cli.harness.adapter_smoke_io import AdapterSmokeResult
from app.cli.harness.bedrock_smoke_io import BedrockSmokeResult
from app.cli.harness.provider_smoke_io import ProviderSmokeResult
from app.services.harness.protocol import (
    ProviderTokenUsage,
    StrictProtocolModel,
)
from app.services.harness.providers.conformance_contracts import (
    ConformanceScenario,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveEvidenceObservation,
    LiveEvidenceStatus,
    LiveProviderDescriptor,
)

type LiveSmokeResult = (
    AdapterSmokeResult | BedrockSmokeResult | ProviderSmokeResult
)


def live_observations_from_smoke(
    descriptor: LiveProviderDescriptor,
    result: LiveSmokeResult,
) -> tuple[LiveEvidenceObservation, ...]:
    scenarios = _scenarios(result)
    _validate_binding(descriptor, result, scenarios)
    trace_sha256 = _model_sha256(result)
    observations: list[LiveEvidenceObservation] = []
    for scenario in scenarios:
        usage = (
            _usage(result)
            if scenario is ConformanceScenario.USAGE_COST
            else None
        )
        charged_cost = (
            result.charged_cost_microusd
            if scenario is ConformanceScenario.USAGE_COST
            else None
        )
        cancellation_latency = (
            result.cancellation_latency_ms
            if (
                isinstance(result, BedrockSmokeResult)
                and scenario is ConformanceScenario.CANCELLATION
            )
            else None
        )
        latency_ms = (
            cancellation_latency
            if cancellation_latency is not None
            else result.latency_ms
        )
        observations.append(
            LiveEvidenceObservation(
                provider=descriptor.provider,
                model_revision_sha256=(
                    descriptor.model_revision_sha256
                ),
                adapter_revision_sha256=(
                    descriptor.adapter_revision_sha256
                ),
                route_binding_sha256=(
                    descriptor.route_binding_sha256
                ),
                scenario=scenario,
                status=LiveEvidenceStatus.PASSED,
                latency_ms=latency_ms,
                cancellation_latency_ms=cancellation_latency,
                usage=usage,
                charged_cost_microusd=charged_cost,
                trace_sha256=trace_sha256,
                reason="Authorized live smoke evidence passed.",
                observed_at=result.completed_at,
            )
        )
    return tuple(observations)


def _scenarios(
    result: LiveSmokeResult,
) -> tuple[ConformanceScenario, ...]:
    if isinstance(result, BedrockSmokeResult):
        return (
            ConformanceScenario.CANCELLATION,
            ConformanceScenario.TEXT_STREAM,
            ConformanceScenario.TOOL_CALLS,
            ConformanceScenario.USAGE_COST,
        )
    return (
        ConformanceScenario.TEXT_STREAM,
        ConformanceScenario.USAGE_COST,
    )


def _validate_binding(
    descriptor: LiveProviderDescriptor,
    result: LiveSmokeResult,
    scenarios: tuple[ConformanceScenario, ...],
) -> None:
    if (
        result.provider != descriptor.provider
        or result.model != descriptor.model
        or result.route_binding_sha256
        != descriptor.route_binding_sha256
        or result.completed_at < descriptor.observed_at
    ):
        raise ValueError("live smoke descriptor binding is invalid")
    if not set(scenarios).issubset(descriptor.supported_scenarios):
        raise ValueError("live smoke claims an unsupported scenario")
    if (
        isinstance(result, ProviderSmokeResult)
        and result.active_credential_leases != 0
    ):
        raise ValueError("live smoke credential cleanup is incomplete")
    if (
        isinstance(result, BedrockSmokeResult)
        and result.charged_cost_microusd
        > result.signed_cost_cap_microusd
    ):
        raise ValueError("live smoke exceeded its signed cost cap")


def _usage(result: LiveSmokeResult) -> ProviderTokenUsage:
    return ProviderTokenUsage(
        input_tokens=result.input_tokens,
        cached_input_tokens=result.cached_input_tokens,
        output_tokens=result.output_tokens,
        reasoning_tokens=result.reasoning_tokens,
        cost_microusd=result.charged_cost_microusd,
    )


def _model_sha256(model: StrictProtocolModel) -> str:
    content = json.dumps(
        model.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(content).hexdigest()
