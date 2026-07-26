"""Fail-closed composition of signed Bedrock smoke runtime evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.cli.harness.bedrock_smoke_contracts import (
    AdmittedBedrockSmokeLaunch,
    AuthorizedBedrockSmoke,
    BedrockSmokeBinding,
    bedrock_smoke_model_sha256,
    verify_bedrock_smoke_grant,
)
from app.cli.harness.bedrock_smoke_io import (
    load_bedrock_smoke_private_inputs,
    load_bedrock_smoke_signing_key,
)
from app.cli.harness.provider_smoke_io import (
    validate_private_smoke_input,
    validate_provider_smoke_output_path,
)
from app.services.harness.protocol import (
    DataClassification,
    ProviderModelCapabilities,
    ProviderPriceRecord,
    price_is_active,
)
from app.services.harness.providers import (
    AuthorizedBedrockRoute,
    BedrockCredentialSourceKind,
    BedrockIdentityReference,
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
    authorize_bedrock_route,
    load_provider_configuration,
)
from app.services.harness.providers.credential_material import zero_buffer

TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class BedrockSmokeRuntime:
    authorized: AuthorizedBedrockSmoke
    route: AuthorizedBedrockRoute
    identity: BedrockIdentityReference
    model: ProviderModelCapabilities
    price: ProviderPriceRecord


async def load_bedrock_smoke_runtime(
    launch: AdmittedBedrockSmokeLaunch,
    *,
    environment: Mapping[bytes, bytes] | None,
    observed_at: datetime,
) -> BedrockSmokeRuntime:
    await validate_private_smoke_input(launch.configuration_path)
    await validate_provider_smoke_output_path(launch.database_path)
    await validate_provider_smoke_output_path(launch.result_path)
    private_inputs, loaded = await asyncio.gather(
        load_bedrock_smoke_private_inputs(launch),
        load_provider_configuration(launch.configuration_path),
    )
    policy = private_inputs.route_policy
    identity = private_inputs.identity
    route = _configured_route(loaded, policy.route_id)
    authorized_route = authorize_bedrock_route(
        loaded,
        route,
        policy,
        identity,
    )
    account_id = _web_identity_account_id(identity)
    model, price = _model_and_price(
        loaded,
        route,
        observed_at=observed_at,
    )
    _validate_data_policy(loaded, route)
    if (
        authorized_route.cross_region_inference
        or private_inputs.signed_grant.payload.text_max_output_tokens
        > model.max_output_tokens
        or private_inputs.signed_grant.payload.tool_max_output_tokens
        > model.max_output_tokens
    ):
        raise ValueError("Bedrock smoke runtime policy is invalid")
    _validate_signed_cost_caps(
        private_inputs.signed_grant.payload.text_cost_cap_microusd,
        private_inputs.signed_grant.payload.text_max_output_tokens,
        private_inputs.signed_grant.payload.tool_cost_cap_microusd,
        private_inputs.signed_grant.payload.tool_max_output_tokens,
        model,
        price,
    )
    binding = BedrockSmokeBinding(
        configuration_sha256=loaded.content_sha256,
        route_policy_sha256=bedrock_smoke_model_sha256(policy),
        identity_sha256=bedrock_smoke_model_sha256(identity),
        identity_reference_id=identity.identity_reference_id,
        aws_account_id=account_id,
        model_id=authorized_route.model_id,
        region=authorized_route.region,
        destination_sha256=authorized_route.destination_sha256,
    )
    signing_key = load_bedrock_smoke_signing_key(
        launch.signing_key_environment_variable,
        environment,
    )
    try:
        authorized = verify_bedrock_smoke_grant(
            launch,
            private_inputs.signed_grant,
            signing_key,
            binding,
            observed_at=observed_at,
        )
    finally:
        zero_buffer(signing_key)
    return BedrockSmokeRuntime(
        authorized=authorized,
        route=authorized_route,
        identity=identity,
        model=model,
        price=price,
    )


def _configured_route(
    loaded: LoadedProviderConfiguration,
    route_id: str,
) -> ProviderRouteConfiguration:
    routes = tuple(
        route
        for route in loaded.configuration.routes
        if route.route_id == route_id
    )
    if len(routes) != 1:
        raise ValueError("Bedrock smoke route is ambiguous")
    return routes[0]


def _web_identity_account_id(identity: BedrockIdentityReference) -> str:
    role_arn = identity.role_arn
    if (
        identity.source is not BedrockCredentialSourceKind.WEB_IDENTITY
        or role_arn is None
    ):
        raise ValueError("Bedrock smoke requires disposable web identity")
    account_id = role_arn.split("::", maxsplit=1)[1].split(":", maxsplit=1)[0]
    if len(account_id) != 12 or not account_id.isdigit():
        raise ValueError("Bedrock smoke AWS account binding is invalid")
    return account_id


def _model_and_price(
    loaded: LoadedProviderConfiguration,
    route: ProviderRouteConfiguration,
    *,
    observed_at: datetime,
) -> tuple[ProviderModelCapabilities, ProviderPriceRecord]:
    models = tuple(
        model
        for model in loaded.configuration.models
        if (
            model.provider == route.provider
            and model.model == route.model
            and model.model_revision_sha256 == route.model_revision_sha256
        )
    )
    prices = tuple(
        price
        for price in loaded.configuration.prices
        if (
            price.provider == route.provider
            and price.model == route.model
            and price.model_revision_sha256 == route.model_revision_sha256
            and price.region == route.region
            and price.price_version_sha256 == route.price_version_sha256
        )
    )
    if (
        len(models) != 1
        or len(prices) != 1
        or not price_is_active(prices[0], observed_at)
    ):
        raise ValueError("Bedrock smoke model price evidence is invalid")
    return models[0], prices[0]


def _validate_data_policy(
    loaded: LoadedProviderConfiguration,
    route: ProviderRouteConfiguration,
) -> None:
    policies = tuple(
        policy
        for policy in loaded.configuration.data_policies
        if (
            policy.provider == route.provider
            and policy.policy_revision_sha256 == route.policy_revision_sha256
        )
    )
    if len(policies) != 1:
        raise ValueError("Bedrock smoke data policy is ambiguous")
    policy = policies[0]
    if (
        DataClassification.PUBLIC not in policy.accepted_classifications
        or route.region not in policy.allowed_regions
        or policy.retention_days != 0
        or policy.training_enabled
    ):
        raise ValueError("Bedrock smoke data policy is ineligible")


def _validate_signed_cost_caps(
    text_cap_microusd: int,
    text_output_tokens: int,
    tool_cap_microusd: int,
    tool_output_tokens: int,
    model: ProviderModelCapabilities,
    price: ProviderPriceRecord,
) -> None:
    text_maximum = _maximum_call_cost(
        model,
        price,
        text_output_tokens,
    )
    tool_maximum = _maximum_call_cost(
        model,
        price,
        tool_output_tokens,
    )
    if (
        text_maximum > text_cap_microusd
        or tool_maximum > tool_cap_microusd
    ):
        raise ValueError("Bedrock smoke signed cost cap is insufficient")


def _maximum_call_cost(
    model: ProviderModelCapabilities,
    price: ProviderPriceRecord,
    output_tokens: int,
) -> int:
    maximum_input_tokens = model.context_window_tokens - output_tokens
    input_rate = max(
        price.input_microusd_per_million_tokens,
        price.cached_input_microusd_per_million_tokens,
    )
    output_rate = max(
        price.output_microusd_per_million_tokens,
        price.reasoning_microusd_per_million_tokens,
    )
    numerator = (
        maximum_input_tokens * input_rate + output_tokens * output_rate
    )
    return (numerator + TOKENS_PER_MILLION - 1) // TOKENS_PER_MILLION
