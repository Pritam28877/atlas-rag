from datetime import UTC, datetime

import pytest

from app.services.harness.protocol import (
    DataClassification,
    ProviderRequirements,
    ProviderRoute,
    ProviderRouteDecisionRecord,
    ProviderRouteRejectionCode,
    RouteHealth,
)
from app.services.harness.runtime import (
    MAXIMUM_PROVIDER_CANDIDATES,
    ProviderRoutingError,
    ProviderRoutingErrorCode,
    select_provider_route,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
DIGEST = "0" * 64


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


def requirements(**overrides: object) -> ProviderRequirements:
    values: dict[str, object] = {
        "input_tokens": 10_000,
        "reserved_output_tokens": 2_000,
        "required_capabilities": ("reasoning", "tools"),
        "required_input_modalities": ("image", "text"),
        "required_output_modalities": ("text",),
        "required_context_features": ("prompt_cache",),
        "data_classification": DataClassification.CONFIDENTIAL,
        "allowed_regions": ("ap-south-1", "us-east-1"),
        "max_retention_days": 30,
        "allow_training": False,
        "max_cost_microusd": 100_000,
    }
    values.update(overrides)
    return ProviderRequirements.model_validate(values)


def route(**overrides: object) -> ProviderRoute:
    values: dict[str, object] = {
        "route_id": "provider.primary",
        "provider": "provider",
        "model": "model-revision",
        "model_revision_sha256": DIGEST,
        "region": "us-east-1",
        "health": RouteHealth.HEALTHY,
        "priority": 10,
        "capabilities": ("reasoning", "tools"),
        "input_modalities": ("image", "text"),
        "output_modalities": ("text",),
        "context_features": ("prompt_cache",),
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


def decide(
    candidates: list[ProviderRoute],
    *,
    requested: ProviderRequirements | None = None,
) -> ProviderRouteDecisionRecord:
    return select_provider_route(
        provider_decision_id=identifier("pvd"),
        turn_id=identifier("trn"),
        requirements=requested or requirements(),
        candidates=candidates,
        decided_at=NOW,
    )


@pytest.mark.parametrize(
    ("route_overrides", "requirements_overrides", "expected_code"),
    [
        (
            {"capabilities": ("reasoning",)},
            {},
            ProviderRouteRejectionCode.CAPABILITY,
        ),
        (
            {"context_features": ()},
            {},
            ProviderRouteRejectionCode.CONTEXT_FEATURE,
        ),
        (
            {"context_window_tokens": 11_999},
            {},
            ProviderRouteRejectionCode.CONTEXT_WINDOW,
        ),
        (
            {"estimated_cost_microusd": 100_001},
            {},
            ProviderRouteRejectionCode.COST,
        ),
        (
            {
                "accepted_data_classifications": (
                    DataClassification.PUBLIC,
                )
            },
            {},
            ProviderRouteRejectionCode.DATA_CLASSIFICATION,
        ),
        (
            {"health": RouteHealth.UNAVAILABLE},
            {},
            ProviderRouteRejectionCode.HEALTH,
        ),
        (
            {"input_modalities": ("text",)},
            {},
            ProviderRouteRejectionCode.INPUT_MODALITY,
        ),
        (
            {"max_output_tokens": 1_999},
            {},
            ProviderRouteRejectionCode.OUTPUT_LIMIT,
        ),
        (
            {"output_modalities": ()},
            {},
            ProviderRouteRejectionCode.OUTPUT_MODALITY,
        ),
        (
            {"region": "eu-west-1"},
            {},
            ProviderRouteRejectionCode.REGION,
        ),
        (
            {"retention_days": 31},
            {},
            ProviderRouteRejectionCode.RETENTION,
        ),
        (
            {"training_enabled": True},
            {},
            ProviderRouteRejectionCode.TRAINING,
        ),
    ],
)
def test_router_records_each_rejection_dimension(
    route_overrides: dict[str, object],
    requirements_overrides: dict[str, object],
    expected_code: ProviderRouteRejectionCode,
) -> None:
    rejected_candidate = route(**route_overrides)

    decision = decide(
        [rejected_candidate],
        requested=requirements(**requirements_overrides),
    )

    assert decision.selected_route_id is None
    assert decision.eligible_routes == ()
    assert decision.rejected_routes[0].rejection_codes == (expected_code,)
    assert decision.rejected_routes[0].model_revision_sha256 == DIGEST
    assert decision.rejected_routes[0].health_snapshot_sha256 == "1" * 64
    assert decision.rejected_routes[0].price_version_sha256 == "2" * 64


def test_router_records_all_reasons_in_canonical_order() -> None:
    incompatible = route(
        capabilities=(),
        context_features=(),
        health=RouteHealth.STALE,
        input_modalities=(),
        output_modalities=(),
        region="eu-west-1",
    )

    decision = decide([incompatible])

    expected_codes = tuple(
        sorted(
            {
                ProviderRouteRejectionCode.CAPABILITY,
                ProviderRouteRejectionCode.CONTEXT_FEATURE,
                ProviderRouteRejectionCode.HEALTH,
                ProviderRouteRejectionCode.INPUT_MODALITY,
                ProviderRouteRejectionCode.OUTPUT_MODALITY,
                ProviderRouteRejectionCode.REGION,
            }
        )
    )
    assert decision.rejected_routes[0].rejection_codes == expected_codes
    assert all(
        code.value in decision.rejected_routes[0].reason
        for code in expected_codes
    )


def test_router_ranking_is_stable_and_preserves_input() -> None:
    degraded = route(
        route_id="provider.degraded",
        health=RouteHealth.DEGRADED,
        priority=0,
        estimated_cost_microusd=1,
    )
    higher_priority_number = route(
        route_id="provider.priority",
        priority=20,
        estimated_cost_microusd=1,
    )
    higher_cost = route(
        route_id="provider.expensive",
        estimated_cost_microusd=60_000,
    )
    selected = route(route_id="provider.best")
    candidates = [degraded, higher_priority_number, higher_cost, selected]
    original_order = tuple(candidate.route_id for candidate in candidates)

    first = decide(candidates)
    second = decide(list(reversed(candidates)))

    assert first.selected_route_id == "provider.best"
    assert second.selected_route_id == first.selected_route_id
    assert tuple(candidate.route_id for candidate in candidates) == original_order
    assert "price_version_sha256=" + "2" * 64 in first.selection_reason
    assert tuple(
        candidate.route_id for candidate in first.eligible_routes
    ) == tuple(sorted(original_order))


def test_router_uses_route_id_as_final_tie_breaker() -> None:
    decision = decide(
        [
            route(route_id="provider.zulu"),
            route(route_id="provider.alpha"),
        ]
    )

    assert decision.selected_route_id == "provider.alpha"


def test_router_handles_empty_and_all_rejected_candidate_sets() -> None:
    empty = decide([])
    rejected = decide([route(health=RouteHealth.UNKNOWN)])

    assert empty.selected_route_id is None
    assert empty.rejected_routes == ()
    assert "0 configured route(s) rejected" in empty.selection_reason
    assert rejected.selected_route_id is None
    assert "1 configured route(s) rejected" in rejected.selection_reason


def test_router_rejects_duplicate_and_unrecordable_candidate_sets() -> None:
    duplicate = route()
    with pytest.raises(ProviderRoutingError) as duplicate_error:
        decide([duplicate, duplicate])
    assert duplicate_error.value.code is ProviderRoutingErrorCode.DUPLICATE_ROUTE

    oversized = [
        route(route_id=f"provider.route-{index:02d}")
        for index in range(MAXIMUM_PROVIDER_CANDIDATES + 1)
    ]
    with pytest.raises(ProviderRoutingError) as limit_error:
        decide(oversized)
    assert limit_error.value.code is ProviderRoutingErrorCode.CANDIDATE_LIMIT
