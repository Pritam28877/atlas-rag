"""Exact regional authorization for Google Vertex Gemini routes."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol import (
    ProviderCredentialHandle,
    Region,
    RouteId,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.providers.config_contracts import (
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
)
from app.services.harness.providers.egress_policy import (
    canonical_provider_url,
    provider_destination_sha256,
)
from app.services.harness.providers.vertex_identity import (
    GcpProjectId,
    VertexIdentityReference,
    VertexIdentityReferenceId,
)

VertexModelId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    ),
]


class VertexRoutePolicy(StrictProtocolModel):
    route_id: RouteId
    project_id: GcpProjectId
    location: Region
    model_id: VertexModelId
    credential_handle: ProviderCredentialHandle
    identity_reference_id: VertexIdentityReferenceId
    endpoint_url: str = Field(min_length=9, max_length=2_048)
    destination_sha256: Sha256
    allowed_locations: tuple[Region, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        endpoint = canonical_provider_url(self.endpoint_url)
        expected_endpoint = (
            f"https://{self.location}-aiplatform.googleapis.com/"
        )
        if (
            endpoint != self.endpoint_url
            or urlsplit(endpoint).path != "/"
            or endpoint != expected_endpoint
        ):
            raise ValueError("Vertex endpoint does not match its location")
        if self.location == "global":
            raise ValueError("Vertex route must use a regional location")
        if provider_destination_sha256(endpoint) != self.destination_sha256:
            raise ValueError("Vertex endpoint hash does not match")
        if self.allowed_locations != (self.location,):
            raise ValueError("Vertex route must be bound to exactly one location")
        return self

    def stream_url(self) -> str:
        model_resource = (
            f"projects/{self.project_id}/locations/{self.location}/"
            f"publishers/google/models/{self.model_id}"
        )
        return f"{self.endpoint_url}v1/{model_resource}:streamGenerateContent"


class AuthorizedVertexRoute(StrictProtocolModel):
    provider: Literal["vertex"] = "vertex"
    route_id: RouteId
    project_id: GcpProjectId
    location: Region
    model_id: VertexModelId
    credential_handle: ProviderCredentialHandle
    identity_reference_id: VertexIdentityReferenceId
    endpoint_url: str
    stream_url: str
    destination_sha256: Sha256


def authorize_vertex_route(
    loaded: LoadedProviderConfiguration,
    route: ProviderRouteConfiguration,
    policy: VertexRoutePolicy,
    identity: VertexIdentityReference,
) -> AuthorizedVertexRoute:
    bindings = {
        binding.handle: binding for binding in loaded.configuration.credential_bindings
    }
    binding = bindings.get(route.credential_handle)
    if (
        not route.enabled
        or route.provider != "vertex"
        or route.route_id != policy.route_id
        or route.model != policy.model_id
        or route.region != policy.location
        or route.credential_handle != policy.credential_handle
        or identity.identity_reference_id != policy.identity_reference_id
        or identity.credential_handle != route.credential_handle
        or identity.quota_project_id != policy.project_id
        or binding is None
        or binding.provider != "vertex"
        or binding.destination_sha256 != policy.destination_sha256
    ):
        raise ValueError("Vertex route authorization failed")
    return AuthorizedVertexRoute(
        route_id=route.route_id,
        project_id=policy.project_id,
        location=policy.location,
        model_id=policy.model_id,
        credential_handle=route.credential_handle,
        identity_reference_id=identity.identity_reference_id,
        endpoint_url=policy.endpoint_url,
        stream_url=policy.stream_url(),
        destination_sha256=policy.destination_sha256,
    )
