import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.harness.protocol import (
    MAXIMUM_PROVIDER_DECISION_ROUTES,
    DataClassification,
    ProviderHealthStatus,
    ProviderRequirements,
    ProviderRouteHealthSnapshot,
    RouteHealth,
    build_provider_health_snapshot,
)
from app.services.harness.providers import (
    ConfiguredRouteInventory,
    ConfiguredRouteInventoryError,
    ConfiguredRouteInventoryErrorCode,
    LoadedProviderConfiguration,
    ProviderConfiguration,
    ProviderHealthRegistry,
)
from tests.harness_configured_catalog_fixtures import (
    configured_catalog_fixture,
)

NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
ROUTE_IDS = (
    "configured.primary",
    "configured.secondary",
    "secondary.primary",
)


def requirements(**overrides: object) -> ProviderRequirements:
    values: dict[str, object] = {
        "input_tokens": 1_000,
        "reserved_output_tokens": 500,
        "required_capabilities": (),
        "required_input_modalities": ("text",),
        "required_output_modalities": ("text",),
        "required_context_features": ("prompt_cache",),
        "data_classification": DataClassification.CONFIDENTIAL,
        "allowed_regions": ("eu-west-1", "us-east-1"),
        "max_retention_days": 0,
        "allow_training": False,
        "max_cost_microusd": 10_000_000_000,
    }
    values.update(overrides)
    return ProviderRequirements.model_validate(values)


def with_configuration_updates(
    loaded: LoadedProviderConfiguration,
    *,
    prices: list[dict[str, object]] | None = None,
    routes: list[dict[str, object]] | None = None,
) -> LoadedProviderConfiguration:
    values = loaded.configuration.model_dump(mode="python")
    if prices is not None:
        values["prices"] = tuple(prices)
    if routes is not None:
        values["routes"] = tuple(routes)
    return LoadedProviderConfiguration(
        content_sha256=loaded.content_sha256,
        configuration=ProviderConfiguration.model_validate(values),
    )


def healthy_snapshot(
    initial: ProviderRouteHealthSnapshot,
) -> ProviderRouteHealthSnapshot:
    observed_at = initial.observed_at + timedelta(seconds=1)
    return build_provider_health_snapshot(
        route_id=initial.route_id,
        status=ProviderHealthStatus.HEALTHY,
        reason="Provider health probe succeeded.",
        observed_at=observed_at,
        valid_until=observed_at + timedelta(seconds=30),
        consecutive_failures=0,
        latency_p95_ms=20,
        previous_snapshot_sha256=initial.snapshot_sha256,
    )


def test_inventory_builds_complete_secret_free_candidates() -> None:
    async def scenario() -> None:
        loaded = configured_catalog_fixture()
        registry = ProviderHealthRegistry(ROUTE_IDS, observed_at=NOW)
        inventory = ConfiguredRouteInventory(loaded, registry)

        candidates = await inventory.candidates(
            requirements(),
            observed_at=NOW,
        )

        assert tuple(candidate.route_id for candidate in candidates) == ROUTE_IDS
        assert all(candidate.health is RouteHealth.UNKNOWN for candidate in candidates)
        assert all(candidate.price_active for candidate in candidates)
        assert candidates[0].estimated_cost_microusd == 2_000
        assert candidates[0].input_modalities == ("text",)
        assert candidates[0].context_features == ("prompt_cache",)
        serialized = " ".join(
            candidate.model_dump_json() for candidate in candidates
        )
        assert "pcr_" not in serialized
        assert "credential" not in serialized

    asyncio.run(scenario())


def test_inventory_preserves_health_price_and_conservative_cost_evidence() -> None:
    async def scenario() -> None:
        loaded = configured_catalog_fixture()
        price_values = [
            price.model_dump(mode="python")
            for price in loaded.configuration.prices
        ]
        price_values[0]["reasoning_microusd_per_million_tokens"] = 4_000_000
        price_values[0]["expires_at"] = NOW + timedelta(seconds=1)
        revised = with_configuration_updates(loaded, prices=price_values)
        registry = ProviderHealthRegistry(ROUTE_IDS, observed_at=NOW)
        initial = await registry.snapshot(
            "configured.primary",
            observed_at=NOW,
        )
        healthy = healthy_snapshot(initial)
        await registry.record(healthy)
        inventory = ConfiguredRouteInventory(revised, registry)

        candidates = await inventory.candidates(
            requirements(),
            observed_at=NOW + timedelta(seconds=1),
        )
        primary = candidates[0]

        assert primary.health is RouteHealth.HEALTHY
        assert primary.health_snapshot_sha256 == healthy.snapshot_sha256
        assert primary.price_version_sha256 == "7" * 64
        assert primary.price_active is False
        assert primary.estimated_cost_microusd == 3_000

    asyncio.run(scenario())


def test_inventory_represents_the_maximum_conservative_cost() -> None:
    async def scenario() -> None:
        loaded = configured_catalog_fixture()
        price_values = [
            price.model_dump(mode="python")
            for price in loaded.configuration.prices
        ]
        for rate_name in (
            "input_microusd_per_million_tokens",
            "output_microusd_per_million_tokens",
            "reasoning_microusd_per_million_tokens",
        ):
            price_values[0][rate_name] = 10_000_000_000
        revised = with_configuration_updates(loaded, prices=price_values)
        registry = ProviderHealthRegistry(ROUTE_IDS, observed_at=NOW)
        inventory = ConfiguredRouteInventory(revised, registry)

        candidates = await inventory.candidates(
            requirements(
                input_tokens=2_000_000,
                reserved_output_tokens=512_000,
            ),
            observed_at=NOW,
        )

        assert candidates[0].estimated_cost_microusd == 25_120_000_000

    asyncio.run(scenario())


def test_inventory_excludes_explicitly_disabled_routes() -> None:
    async def scenario() -> None:
        loaded = configured_catalog_fixture()
        route_values = [
            route.model_dump(mode="python")
            for route in loaded.configuration.routes
        ]
        route_values[1]["enabled"] = False
        revised = with_configuration_updates(loaded, routes=route_values)
        enabled_route_ids = (
            "configured.primary",
            "secondary.primary",
        )
        registry = ProviderHealthRegistry(
            enabled_route_ids,
            observed_at=NOW,
        )
        inventory = ConfiguredRouteInventory(revised, registry)

        candidates = await inventory.candidates(
            requirements(),
            observed_at=NOW,
        )

        assert tuple(
            candidate.route_id for candidate in candidates
        ) == enabled_route_ids

    asyncio.run(scenario())


def test_inventory_rejects_more_routes_than_a_decision_can_record() -> None:
    loaded = configured_catalog_fixture()
    template = loaded.configuration.routes[0].model_dump(mode="python")
    route_values = []
    for index in range(MAXIMUM_PROVIDER_DECISION_ROUTES + 1):
        route_values.append(
            {
                **template,
                "route_id": f"configured.route-{index:03d}",
            }
        )
    oversized = with_configuration_updates(loaded, routes=route_values)
    registry = ProviderHealthRegistry(
        ("configured.route-000",),
        observed_at=NOW,
    )

    with pytest.raises(ConfiguredRouteInventoryError) as captured:
        ConfiguredRouteInventory(oversized, registry)

    assert captured.value.code is ConfiguredRouteInventoryErrorCode.ROUTE_LIMIT
