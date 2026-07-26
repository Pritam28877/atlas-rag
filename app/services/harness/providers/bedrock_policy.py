"""Region, model, endpoint, and credential binding for Bedrock routes."""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    Region,
    RouteId,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers.bedrock_identity import (
    BedrockIdentityReference,
    BedrockIdentityReferenceId,
)
from app.services.harness.providers.config_contracts import (
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
)
from app.services.harness.providers.egress_policy import (
    canonical_provider_url,
    provider_destination_sha256,
)


class BedrockRoutePolicy(StrictProtocolModel):
    route_id: RouteId
    model_id: ModelName
    region: Region
    credential_handle: ProviderCredentialHandle
    identity_reference_id: BedrockIdentityReferenceId
    endpoint_url: str = Field(min_length=9, max_length=2_048)
    destination_sha256: Sha256
    allowed_inference_regions: tuple[Region, ...] = Field(
        min_length=1,
        max_length=32,
    )
    allow_cross_region_inference: bool = False

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        endpoint = canonical_provider_url(self.endpoint_url)
        if endpoint != self.endpoint_url or urlsplit(endpoint).path != "/":
            raise ValueError("Bedrock endpoint must be a canonical origin")
        expected_hosts = {
            f"bedrock-runtime.{self.region}.amazonaws.com",
            f"bedrock-runtime-fips.{self.region}.amazonaws.com",
            f"bedrock-runtime.{self.region}.amazonaws.com.cn",
        }
        if urlsplit(endpoint).hostname not in expected_hosts:
            raise ValueError("Bedrock endpoint does not match its region")
        if provider_destination_sha256(endpoint) != self.destination_sha256:
            raise ValueError("Bedrock endpoint hash does not match")
        regions = self.allowed_inference_regions
        if tuple(sorted(set(regions))) != regions or self.region not in regions:
            raise ValueError("Bedrock inference regions are not canonical")
        if not self.allow_cross_region_inference and regions != (self.region,):
            raise ValueError("regional Bedrock route cannot allow other regions")
        return self


class AuthorizedBedrockRoute(StrictProtocolModel):
    provider: Literal["bedrock"] = "bedrock"
    route_id: RouteId
    model_id: ModelName
    region: Region
    credential_handle: ProviderCredentialHandle
    identity_reference_id: BedrockIdentityReferenceId
    endpoint_url: str
    destination_sha256: Sha256
    allowed_inference_regions: tuple[Region, ...]
    cross_region_inference: bool


def authorize_bedrock_route(
    loaded: LoadedProviderConfiguration,
    route: ProviderRouteConfiguration,
    policy: BedrockRoutePolicy,
    identity: BedrockIdentityReference,
) -> AuthorizedBedrockRoute:
    bindings = {
        binding.handle: binding for binding in loaded.configuration.credential_bindings
    }
    binding = bindings.get(route.credential_handle)
    if (
        not route.enabled
        or route.provider != "bedrock"
        or route.route_id != policy.route_id
        or route.model != policy.model_id
        or route.region != policy.region
        or route.credential_handle != policy.credential_handle
        or identity.identity_reference_id != policy.identity_reference_id
        or identity.credential_handle != route.credential_handle
        or binding is None
        or binding.provider != "bedrock"
        or binding.destination_sha256 != policy.destination_sha256
    ):
        raise ValueError("Bedrock route authorization failed")
    return AuthorizedBedrockRoute(
        route_id=route.route_id,
        model_id=route.model,
        region=route.region,
        credential_handle=route.credential_handle,
        identity_reference_id=identity.identity_reference_id,
        endpoint_url=policy.endpoint_url,
        destination_sha256=policy.destination_sha256,
        allowed_inference_regions=policy.allowed_inference_regions,
        cross_region_inference=policy.allow_cross_region_inference,
    )
