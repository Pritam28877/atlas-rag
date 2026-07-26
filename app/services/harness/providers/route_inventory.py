"""Compile validated provider configuration into bounded routing candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.services.harness.protocol import (
    MAXIMUM_PROVIDER_DECISION_ROUTES,
    ProviderDataPolicy,
    ProviderHealthStatus,
    ProviderModelCapabilities,
    ProviderPriceRecord,
    ProviderRequirements,
    ProviderRoute,
    RouteHealth,
    price_is_active,
)
from app.services.harness.providers.config_contracts import (
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
)
from app.services.harness.providers.health_registry import (
    ProviderHealthRegistry,
)

TOKENS_PER_MILLION = 1_000_000


class ConfiguredRouteInventoryErrorCode(StrEnum):
    ROUTE_LIMIT = "route_limit"


class ConfiguredRouteInventoryError(RuntimeError):
    def __init__(self, code: ConfiguredRouteInventoryErrorCode) -> None:
        super().__init__("configured provider routes cannot be inventoried")
        self.code = code


@dataclass(frozen=True, slots=True)
class _ConfiguredRouteParts:
    route: ProviderRouteConfiguration
    model: ProviderModelCapabilities
    policy: ProviderDataPolicy
    price: ProviderPriceRecord
    destination_sha256: str


class ConfiguredRouteInventory:
    """Pre-indexes secret-free route metadata for repeated turn decisions."""

    def __init__(
        self,
        loaded: LoadedProviderConfiguration,
        health_registry: ProviderHealthRegistry,
    ) -> None:
        configuration = loaded.configuration
        enabled_routes = tuple(
            route for route in configuration.routes if route.enabled
        )
        if len(enabled_routes) > MAXIMUM_PROVIDER_DECISION_ROUTES:
            raise ConfiguredRouteInventoryError(
                ConfiguredRouteInventoryErrorCode.ROUTE_LIMIT
            )

        models = {
            (model.provider, model.model, model.model_revision_sha256): model
            for model in configuration.models
        }
        credentials = {
            credential.handle: credential
            for credential in configuration.credential_bindings
        }
        policies = {
            (policy.provider, policy.policy_revision_sha256): policy
            for policy in configuration.data_policies
        }
        prices = {
            (
                price.provider,
                price.model,
                price.model_revision_sha256,
                price.region,
                price.price_version_sha256,
            ): price
            for price in configuration.prices
        }
        self._routes = tuple(
            _ConfiguredRouteParts(
                route=route,
                model=models[
                    (route.provider, route.model, route.model_revision_sha256)
                ],
                policy=policies[
                    (route.provider, route.policy_revision_sha256)
                ],
                price=prices[
                    (
                        route.provider,
                        route.model,
                        route.model_revision_sha256,
                        route.region,
                        route.price_version_sha256,
                    )
                ],
                destination_sha256=credentials[
                    route.credential_handle
                ].destination_sha256,
            )
            for route in enabled_routes
        )
        self._health_registry = health_registry

    async def candidates(
        self,
        requirements: ProviderRequirements,
        *,
        observed_at: datetime,
    ) -> tuple[ProviderRoute, ...]:
        candidates: list[ProviderRoute] = []
        for parts in self._routes:
            health_snapshot = await self._health_registry.snapshot(
                parts.route.route_id,
                observed_at=observed_at,
            )
            candidates.append(
                ProviderRoute(
                    route_id=parts.route.route_id,
                    provider=parts.route.provider,
                    model=parts.route.model,
                    model_revision_sha256=(
                        parts.route.model_revision_sha256
                    ),
                    region=parts.route.region,
                    health=_route_health(health_snapshot.status),
                    priority=parts.route.priority,
                    capabilities=parts.model.capabilities,
                    input_modalities=tuple(
                        modality.value
                        for modality in parts.model.input_modalities
                    ),
                    output_modalities=tuple(
                        modality.value
                        for modality in parts.model.output_modalities
                    ),
                    context_features=tuple(
                        feature.value
                        for feature in parts.model.context_features
                    ),
                    accepted_data_classifications=(
                        parts.policy.accepted_classifications
                    ),
                    retention_days=parts.policy.retention_days,
                    training_enabled=parts.policy.training_enabled,
                    destination_sha256=parts.destination_sha256,
                    context_window_tokens=(
                        parts.model.context_window_tokens
                    ),
                    max_output_tokens=parts.model.max_output_tokens,
                    estimated_cost_microusd=_estimated_cost(
                        requirements,
                        parts.price,
                    ),
                    health_snapshot_sha256=(
                        health_snapshot.snapshot_sha256
                    ),
                    price_version_sha256=(
                        parts.price.price_version_sha256
                    ),
                    price_active=price_is_active(
                        parts.price,
                        observed_at,
                    ),
                )
            )
        return tuple(candidates)


def _route_health(status: ProviderHealthStatus) -> RouteHealth:
    return RouteHealth(status.value)


def _estimated_cost(
    requirements: ProviderRequirements,
    price: ProviderPriceRecord,
) -> int:
    input_cost_numerator = (
        requirements.input_tokens
        * price.input_microusd_per_million_tokens
    )
    conservative_output_rate = max(
        price.output_microusd_per_million_tokens,
        price.reasoning_microusd_per_million_tokens,
    )
    output_cost_numerator = (
        requirements.reserved_output_tokens * conservative_output_rate
    )
    total_cost_numerator = input_cost_numerator + output_cost_numerator
    return (
        total_cost_numerator + TOKENS_PER_MILLION - 1
    ) // TOKENS_PER_MILLION
