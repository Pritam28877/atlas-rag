"""Policy-complete fixtures for provider failover tests."""

from datetime import UTC, datetime

from app.services.harness.protocol import (
    DataClassification,
    ProviderFailureClass,
    ProviderRequirements,
    ProviderRetryDisposition,
    ProviderRoute,
    ProviderRouteDecisionRecord,
    ProviderTransportFailure,
    RouteHealth,
)
from app.services.harness.runtime import select_provider_route

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
REQUEST_SHA256 = "9" * 64


def route_decision() -> ProviderRouteDecisionRecord:
    return select_provider_route(
        provider_decision_id="pvd_" + "1" * 32,
        turn_id="trn_" + "2" * 32,
        requirements=requirements(),
        candidates=(
            route(
                route_id="openai.primary",
                provider="openai",
                priority=0,
                estimated_cost_microusd=50,
            ),
            route(
                route_id="bedrock.secondary",
                provider="bedrock",
                priority=10,
                estimated_cost_microusd=40,
            ),
            route(
                route_id="vertex.tertiary",
                provider="vertex",
                priority=20,
                estimated_cost_microusd=30,
            ),
            route(
                route_id="openrouter.region",
                provider="openrouter",
                region="eu-west-1",
            ),
            route(
                route_id="local.policy",
                provider="local-compatible",
                accepted_data_classifications=(DataClassification.PUBLIC,),
            ),
            route(
                route_id="mock.incompatible",
                provider="mock",
                capabilities=(),
                estimated_cost_microusd=101,
            ),
        ),
        decided_at=NOW,
    )


def requirements() -> ProviderRequirements:
    return ProviderRequirements(
        input_tokens=100,
        reserved_output_tokens=50,
        required_capabilities=("tools",),
        required_input_modalities=("text",),
        required_output_modalities=("text",),
        required_context_features=(),
        data_classification=DataClassification.CONFIDENTIAL,
        allowed_regions=("us-east-1",),
        max_retention_days=0,
        allow_training=False,
        max_cost_microusd=100,
    )


def route(
    *,
    route_id: str,
    provider: str,
    priority: int = 100,
    region: str = "us-east-1",
    capabilities: tuple[str, ...] = ("tools",),
    accepted_data_classifications: tuple[DataClassification, ...] = (
        DataClassification.CONFIDENTIAL,
    ),
    estimated_cost_microusd: int = 60,
) -> ProviderRoute:
    return ProviderRoute(
        route_id=route_id,
        provider=provider,
        model="model",
        model_revision_sha256="3" * 64,
        region=region,
        health=RouteHealth.HEALTHY,
        priority=priority,
        capabilities=capabilities,
        input_modalities=("text",),
        output_modalities=("text",),
        context_features=(),
        accepted_data_classifications=accepted_data_classifications,
        retention_days=0,
        training_enabled=False,
        destination_sha256="4" * 64,
        context_window_tokens=10_000,
        max_output_tokens=1_000,
        estimated_cost_microusd=estimated_cost_microusd,
        health_snapshot_sha256="5" * 64,
        price_version_sha256="6" * 64,
    )


def failure(
    *,
    failure_class: ProviderFailureClass = ProviderFailureClass.TRANSIENT,
    disposition: ProviderRetryDisposition = ProviderRetryDisposition.ELIGIBLE,
    ambiguous: bool = False,
    request_sha256: str = REQUEST_SHA256,
) -> ProviderTransportFailure:
    return ProviderTransportFailure(
        failure_class=failure_class,
        retry_disposition=disposition,
        code="synthetic_failover_failure",
        reason="Synthetic failover failure.",
        provider_request_sha256=request_sha256,
        ambiguous=ambiguous,
        occurred_at=NOW,
    )
