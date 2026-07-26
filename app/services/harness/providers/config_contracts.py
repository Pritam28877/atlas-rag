"""Strict bounded provider configuration with complete cross references."""

from __future__ import annotations

from typing import Literal, Self, TypeVar

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    ProviderDataPolicy,
    ProviderModelCapabilities,
    ProviderName,
    ProviderPriceRecord,
    Region,
    RouteId,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.routing import ModelName

MAXIMUM_CONFIGURED_MODELS = 1_024
MAXIMUM_CREDENTIAL_BINDINGS = 256
MAXIMUM_DATA_POLICIES = 256
MAXIMUM_PRICE_RECORDS = 4_096
MAXIMUM_CONFIGURED_ROUTES = 1_024
CanonicalKeyT = TypeVar(
    "CanonicalKeyT",
    str,
    tuple[str, str],
    tuple[str, str, str],
    tuple[str, str, str, str, str],
)


class ProviderCredentialBinding(StrictProtocolModel):
    handle: ProviderCredentialHandle
    provider: ProviderName
    destination_sha256: Sha256


class ProviderRouteConfiguration(StrictProtocolModel):
    route_id: RouteId
    provider: ProviderName
    model: ModelName
    model_revision_sha256: Sha256
    region: Region
    credential_handle: ProviderCredentialHandle
    policy_revision_sha256: Sha256
    price_version_sha256: Sha256
    priority: int = Field(ge=0, le=1_000_000)
    enabled: bool


class ProviderConfiguration(StrictProtocolModel):
    schema_version: Literal[1]
    models: tuple[ProviderModelCapabilities, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CONFIGURED_MODELS,
    )
    credential_bindings: tuple[ProviderCredentialBinding, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CREDENTIAL_BINDINGS,
    )
    data_policies: tuple[ProviderDataPolicy, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_DATA_POLICIES,
    )
    prices: tuple[ProviderPriceRecord, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_PRICE_RECORDS,
    )
    routes: tuple[ProviderRouteConfiguration, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CONFIGURED_ROUTES,
    )

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        models = self._models_by_key()
        credentials = self._credentials_by_handle()
        policies = self._policies_by_key()
        prices = self._prices_by_key()
        self._require_canonical_routes()
        enabled_routes = 0
        for route in self.routes:
            enabled_routes += int(route.enabled)
            model = models.get(
                (
                    route.provider,
                    route.model,
                    route.model_revision_sha256,
                )
            )
            if model is None:
                raise ValueError(
                    f"route {route.route_id} references an unknown model revision"
                )
            if route.region not in model.available_regions:
                raise ValueError(
                    f"route {route.route_id} uses an unavailable model region"
                )
            credential = credentials.get(route.credential_handle)
            if credential is None:
                raise ValueError(
                    f"route {route.route_id} references an unknown credential handle"
                )
            if credential.provider != route.provider:
                raise ValueError(
                    f"route {route.route_id} credential provider does not match"
                )
            policy = policies.get(
                (route.provider, route.policy_revision_sha256)
            )
            if policy is None:
                raise ValueError(
                    f"route {route.route_id} references an unknown data policy"
                )
            if policy.destination_sha256 != credential.destination_sha256:
                raise ValueError(
                    f"route {route.route_id} destination binding does not match"
                )
            if route.region not in policy.allowed_regions:
                raise ValueError(
                    f"route {route.route_id} violates policy region"
                )
            price_key = (
                route.provider,
                route.model,
                route.model_revision_sha256,
                route.region,
                route.price_version_sha256,
            )
            if price_key not in prices:
                raise ValueError(
                    f"route {route.route_id} references an unknown price revision"
                )
        if enabled_routes == 0:
            raise ValueError("provider configuration requires an enabled route")
        return self

    def _models_by_key(
        self,
    ) -> dict[tuple[str, str, str], ProviderModelCapabilities]:
        models: dict[tuple[str, str, str], ProviderModelCapabilities] = {}
        provider_snapshots: dict[str, str] = {}
        keys: list[tuple[str, str, str]] = []
        for model in self.models:
            key = (
                model.provider,
                model.model,
                model.model_revision_sha256,
            )
            keys.append(key)
            models[key] = model
            provider_snapshot = provider_snapshots.get(model.provider)
            if (
                provider_snapshot is not None
                and provider_snapshot != model.catalog_snapshot_sha256
            ):
                raise ValueError(
                    f"provider {model.provider} has mixed catalog snapshots"
                )
            provider_snapshots[model.provider] = (
                model.catalog_snapshot_sha256
            )
        self._require_unique_sorted(tuple(keys), "configured models")
        return models

    def _credentials_by_handle(
        self,
    ) -> dict[str, ProviderCredentialBinding]:
        credentials: dict[str, ProviderCredentialBinding] = {}
        keys: list[str] = []
        for credential in self.credential_bindings:
            keys.append(credential.handle)
            credentials[credential.handle] = credential
        self._require_unique_sorted(
            tuple(keys),
            "credential bindings",
        )
        return credentials

    def _policies_by_key(
        self,
    ) -> dict[tuple[str, str], ProviderDataPolicy]:
        policies: dict[tuple[str, str], ProviderDataPolicy] = {}
        keys: list[tuple[str, str]] = []
        for policy in self.data_policies:
            key = (policy.provider, policy.policy_revision_sha256)
            keys.append(key)
            policies[key] = policy
        self._require_unique_sorted(tuple(keys), "data policies")
        return policies

    def _prices_by_key(
        self,
    ) -> dict[tuple[str, str, str, str, str], ProviderPriceRecord]:
        prices: dict[
            tuple[str, str, str, str, str],
            ProviderPriceRecord,
        ] = {}
        keys: list[tuple[str, str, str, str, str]] = []
        for price in self.prices:
            key = (
                price.provider,
                price.model,
                price.model_revision_sha256,
                price.region,
                price.price_version_sha256,
            )
            keys.append(key)
            prices[key] = price
        self._require_unique_sorted(tuple(keys), "provider prices")
        return prices

    def _require_canonical_routes(self) -> None:
        route_ids = tuple(route.route_id for route in self.routes)
        self._require_unique_sorted(route_ids, "provider routes")

    @staticmethod
    def _require_unique_sorted(
        values: tuple[CanonicalKeyT, ...],
        label: str,
    ) -> None:
        if tuple(sorted(set(values))) != values:
            raise ValueError(f"{label} must be unique and sorted")


class LoadedProviderConfiguration(StrictProtocolModel):
    content_sha256: Sha256
    configuration: ProviderConfiguration
