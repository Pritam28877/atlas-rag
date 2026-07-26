from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    DataClassification,
    ProviderRequirements,
    ProviderRoute,
    ProviderRouteDecisionRecord,
    ProviderRouteRejectionCode,
    RejectedProviderRoute,
    RouteHealth,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
DIGEST = "0" * 64


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


def requirements() -> ProviderRequirements:
    return ProviderRequirements(
        input_tokens=10_000,
        reserved_output_tokens=2_000,
        required_capabilities=("reasoning", "tools"),
        data_classification=DataClassification.CONFIDENTIAL,
        allowed_regions=("ap-south-1", "us-east-1"),
        max_retention_days=30,
        allow_training=False,
        max_cost_microusd=100_000,
    )


def route(**overrides: object) -> ProviderRoute:
    values: dict[str, object] = {
        "route_id": "openai.primary",
        "provider": "openai",
        "model": "model-revision",
        "model_revision_sha256": DIGEST,
        "region": "us-east-1",
        "health": RouteHealth.HEALTHY,
        "capabilities": ("reasoning", "tools"),
        "accepted_data_classifications": (
            DataClassification.CONFIDENTIAL,
            DataClassification.INTERNAL,
        ),
        "retention_days": 30,
        "training_enabled": False,
        "destination_sha256": "3" * 64,
        "context_window_tokens": 128_000,
        "max_output_tokens": 16_000,
        "estimated_cost_microusd": 50_000,
        "health_snapshot_sha256": "1" * 64,
        "price_version_sha256": "2" * 64,
    }
    values.update(overrides)
    return ProviderRoute.model_validate(values)


def decision(**overrides: object) -> ProviderRouteDecisionRecord:
    values: dict[str, object] = {
        "provider_decision_id": identifier("pvd"),
        "turn_id": identifier("trn"),
        "requirements": requirements(),
        "eligible_routes": (route(),),
        "rejected_routes": (
            RejectedProviderRoute(
                route_id="bedrock.secondary",
                reason="Required model capability is unavailable.",
            ),
        ),
        "selected_route_id": "openai.primary",
        "selection_reason": "Lowest healthy cost inside the allowed policy.",
        "decided_at": NOW,
    }
    values.update(overrides)
    return ProviderRouteDecisionRecord.model_validate(values)


def test_provider_decision_records_eligible_and_eliminated_routes() -> None:
    record = decision()

    assert record.selected_route_id == "openai.primary"
    assert record.rejected_routes[0].route_id == "bedrock.secondary"
    with pytest.raises(ValidationError, match="unique and sorted"):
        decision(eligible_routes=(route(), route()))
    with pytest.raises(ValidationError, match="eligible and rejected"):
        decision(
            rejected_routes=(
                RejectedProviderRoute(
                    route_id="openai.primary",
                    reason="Injected conflicting classification.",
                ),
            )
        )
    with pytest.raises(ValidationError, match="must be eligible"):
        decision(selected_route_id="local.unlisted")


def test_eligible_provider_route_must_satisfy_every_requirement() -> None:
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(region="eu-west-1"),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(context_window_tokens=11_999),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(estimated_cost_microusd=100_001),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(price_active=False),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(capabilities=("reasoning",)),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(training_enabled=True),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(retention_days=31),))
    with pytest.raises(ValidationError, match="violates requirements"):
        decision(eligible_routes=(route(health=RouteHealth.UNKNOWN),))


def test_modality_and_context_requirements_cannot_be_silently_downgraded() -> None:
    requirement_values = requirements().model_dump(mode="python")
    requirement_values.update(
        {
            "required_input_modalities": ("image", "text"),
            "required_output_modalities": ("text",),
            "required_context_features": ("prompt_cache",),
        }
    )
    rich_requirements = ProviderRequirements.model_validate(
        requirement_values
    )
    rich_route = route(
        input_modalities=("image", "text"),
        output_modalities=("text",),
        context_features=("prompt_cache",),
    )

    assert decision(
        requirements=rich_requirements,
        eligible_routes=(rich_route,),
    ).selected_route_id == rich_route.route_id
    for field_name in (
        "input_modalities",
        "output_modalities",
        "context_features",
    ):
        route_values = rich_route.model_dump(mode="python")
        route_values[field_name] = ()
        incompatible = ProviderRoute.model_validate(route_values)
        with pytest.raises(ValidationError, match="violates requirements"):
            decision(
                requirements=rich_requirements,
                eligible_routes=(incompatible,),
            )


def test_rejected_route_codes_and_revision_evidence_are_canonical() -> None:
    rejected = RejectedProviderRoute(
        route_id="configured.rejected",
        reason="Required input modality is unavailable.",
        rejection_codes=(
            ProviderRouteRejectionCode.HEALTH,
            ProviderRouteRejectionCode.INPUT_MODALITY,
        ),
        provider="configured-provider",
        model="configured-model",
        model_revision_sha256="1" * 64,
        health=RouteHealth.UNAVAILABLE,
        health_snapshot_sha256="2" * 64,
        price_version_sha256="3" * 64,
    )

    assert rejected.price_version_sha256 == "3" * 64
    with pytest.raises(ValidationError, match="unique and sorted"):
        RejectedProviderRoute.model_validate(
            {
                **rejected.model_dump(mode="python"),
                "rejection_codes": (
                    ProviderRouteRejectionCode.INPUT_MODALITY,
                    ProviderRouteRejectionCode.HEALTH,
                ),
            }
        )
    with pytest.raises(ValidationError, match="must be complete"):
        RejectedProviderRoute(
            route_id="configured.partial",
            reason="Incomplete injected evidence.",
            provider="configured-provider",
        )


def test_all_rejected_provider_decision_has_no_selected_route() -> None:
    record = decision(
        eligible_routes=(),
        selected_route_id=None,
    )

    assert record.eligible_routes == ()
    with pytest.raises(ValidationError, match="requires a selected route"):
        decision(selected_route_id=None)
