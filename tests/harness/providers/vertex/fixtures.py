from pathlib import Path

from app.services.harness.providers import (
    LoadedProviderConfiguration,
    ProviderConfiguration,
    ProviderCredentialBinding,
    ProviderRouteConfiguration,
)
from app.services.harness.providers.egress_policy import (
    provider_destination_sha256,
)
from app.services.harness.providers.vertex_identity import (
    VertexCredentialSourceKind,
    VertexIdentityReference,
)
from app.services.harness.providers.vertex_policy import (
    VertexRoutePolicy,
    authorize_vertex_route,
)
from tests.harness_provider_capability_fixtures import model, policy, price

ENDPOINT = "https://us-central1-aiplatform.googleapis.com/"
DESTINATION_SHA256 = provider_destination_sha256(ENDPOINT)
HANDLE = "pcr_" + "1" * 32
IDENTITY_ID = "gcpid_" + "2" * 32
PROJECT_ID = "atlas-test-12345"
MODEL_ID = "gemini-2.5-flash"


def identity(
    source: VertexCredentialSourceKind = (
        VertexCredentialSourceKind.APPLICATION_DEFAULT
    ),
    **updates: object,
) -> VertexIdentityReference:
    values = {
        "identity_reference_id": IDENTITY_ID,
        "credential_handle": HANDLE,
        "source": source,
        "quota_project_id": PROJECT_ID,
        "expected_principal_sha256": "8" * 64,
        **updates,
    }
    return VertexIdentityReference.model_validate(values)


def route_policy(**updates: object) -> VertexRoutePolicy:
    values = {
        "route_id": "vertex.primary",
        "project_id": PROJECT_ID,
        "location": "us-central1",
        "model_id": MODEL_ID,
        "credential_handle": HANDLE,
        "identity_reference_id": IDENTITY_ID,
        "endpoint_url": ENDPOINT,
        "destination_sha256": DESTINATION_SHA256,
        "allowed_locations": ("us-central1",),
    }
    values.update(updates)
    return VertexRoutePolicy.model_validate(values)


def configuration() -> tuple[
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
]:
    provider_model = model().model_copy(
        update={
            "provider": "vertex",
            "model": MODEL_ID,
            "available_regions": ("us-central1",),
        }
    )
    data_policy = policy().model_copy(
        update={
            "provider": "vertex",
            "destination_sha256": DESTINATION_SHA256,
            "allowed_regions": ("us-central1",),
        }
    )
    provider_price = price().model_copy(
        update={
            "provider": "vertex",
            "model": MODEL_ID,
            "region": "us-central1",
        }
    )
    route = ProviderRouteConfiguration(
        route_id="vertex.primary",
        provider="vertex",
        model=MODEL_ID,
        model_revision_sha256=provider_model.model_revision_sha256,
        region="us-central1",
        credential_handle=HANDLE,
        policy_revision_sha256=data_policy.policy_revision_sha256,
        price_version_sha256=provider_price.price_version_sha256,
        priority=1,
        enabled=True,
    )
    provider_configuration = ProviderConfiguration(
        schema_version=1,
        models=(provider_model,),
        credential_bindings=(
            ProviderCredentialBinding(
                handle=HANDLE,
                provider="vertex",
                destination_sha256=DESTINATION_SHA256,
            ),
        ),
        data_policies=(data_policy,),
        prices=(provider_price,),
        routes=(route,),
    )
    loaded = LoadedProviderConfiguration(
        content_sha256="f" * 64,
        configuration=provider_configuration,
    )
    return loaded, route


def external_account_path() -> Path:
    return Path("/var/run/secrets/google/external-account.json")


def authorized_route():
    loaded, route = configuration()
    return authorize_vertex_route(
        loaded,
        route,
        route_policy(),
        identity(),
    )
