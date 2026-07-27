"""Six-provider live matrix fixtures."""

from datetime import UTC, datetime, timedelta

from app.services.harness.protocol import ProviderTokenUsage
from app.services.harness.providers.conformance_contracts import (
    CONFORMANCE_SCENARIOS,
    ConformanceScenario,
)
from app.services.harness.providers.live_matrix_contracts import (
    LiveEvidenceObservation,
    LiveEvidenceStatus,
    LiveProviderDescriptor,
    LiveProviderEnvironment,
)

NOW = datetime(2026, 7, 28, 14, 0, tzinfo=UTC)
REQUIRED_PROVIDERS = (
    "bedrock",
    "local-compatible",
    "openai",
)


def descriptors() -> tuple[LiveProviderDescriptor, ...]:
    return (
        descriptor("bedrock", LiveProviderEnvironment.CLOUD, "1"),
        descriptor(
            "local-compatible",
            LiveProviderEnvironment.LOCAL,
            "2",
        ),
        descriptor("mock", LiveProviderEnvironment.MOCK, "3"),
        descriptor("openai", LiveProviderEnvironment.CLOUD, "4"),
        descriptor("openrouter", LiveProviderEnvironment.CLOUD, "5"),
        descriptor("vertex", LiveProviderEnvironment.CLOUD, "6"),
    )


def descriptor(
    provider: str,
    environment: LiveProviderEnvironment,
    digest_character: str,
    *,
    supported_scenarios=CONFORMANCE_SCENARIOS,
) -> LiveProviderDescriptor:
    return LiveProviderDescriptor(
        provider=provider,
        environment=environment,
        model="configured-model",
        model_revision_sha256=digest_character * 64,
        adapter_revision_sha256="a" * 64,
        route_binding_sha256="b" * 64,
        supported_scenarios=supported_scenarios,
        observed_at=NOW,
    )


def passed(
    provider: str,
    scenario: ConformanceScenario,
    *,
    cost_microusd: int = 0,
) -> LiveEvidenceObservation:
    provider_descriptor = next(
        item for item in descriptors() if item.provider == provider
    )
    includes_usage = scenario is ConformanceScenario.USAGE_COST
    return LiveEvidenceObservation(
        provider=provider,
        model_revision_sha256=(
            provider_descriptor.model_revision_sha256
        ),
        adapter_revision_sha256=(
            provider_descriptor.adapter_revision_sha256
        ),
        route_binding_sha256=(
            provider_descriptor.route_binding_sha256
        ),
        scenario=scenario,
        status=LiveEvidenceStatus.PASSED,
        latency_ms=10,
        cancellation_latency_ms=(
            2 if scenario is ConformanceScenario.CANCELLATION else None
        ),
        usage=(
            ProviderTokenUsage(
                input_tokens=2,
                cached_input_tokens=0,
                output_tokens=1,
                reasoning_tokens=0,
                cost_microusd=cost_microusd,
            )
            if includes_usage
            else None
        ),
        charged_cost_microusd=(
            cost_microusd if includes_usage else None
        ),
        trace_sha256="c" * 64,
        reason="Live scenario passed.",
        observed_at=NOW + timedelta(seconds=1),
    )
