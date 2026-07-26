from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.harness.providers import (
    LoadedProviderConfiguration,
    ProviderConfiguration,
    ProviderCredentialBinding,
    ProviderRouteConfiguration,
)
from app.services.harness.providers.bedrock_identity import (
    BedrockCredentialSourceKind,
    BedrockIdentityReference,
)
from app.services.harness.providers.bedrock_policy import (
    BedrockRoutePolicy,
    authorize_bedrock_route,
)
from app.services.harness.providers.egress_policy import (
    provider_destination_sha256,
)
from tests.harness_provider_capability_fixtures import model, policy, price

ENDPOINT = "https://bedrock-runtime.us-east-1.amazonaws.com/"
DESTINATION_SHA256 = provider_destination_sha256(ENDPOINT)
HANDLE = "pcr_" + "1" * 32
IDENTITY_ID = "awsid_" + "2" * 32


def test_identity_sources_require_exact_non_secret_fields() -> None:
    environment = identity(BedrockCredentialSourceKind.ENVIRONMENT)
    profile = identity(
        BedrockCredentialSourceKind.PROFILE,
        profile_name="atlas-test",
    )
    web_identity = identity(
        BedrockCredentialSourceKind.WEB_IDENTITY,
        role_arn="arn:aws:iam::123456789012:role/atlas-bedrock",
        web_identity_token_file=Path("/var/run/secrets/atlas-token"),
        role_session_name="atlas-session",
    )

    assert environment.profile_name is None
    assert profile.profile_name == "atlas-test"
    assert web_identity.role_arn is not None
    with pytest.raises(ValidationError, match="inconsistent"):
        identity(
            BedrockCredentialSourceKind.INSTANCE_METADATA,
            profile_name="fallback-must-not-be-possible",
        )
    with pytest.raises(ValidationError, match="inconsistent"):
        identity(
            BedrockCredentialSourceKind.WEB_IDENTITY,
            role_arn="arn:aws:iam::123456789012:role/atlas-bedrock",
            web_identity_token_file=Path("relative-token"),
            role_session_name="atlas-session",
        )


def test_route_authorization_binds_region_model_endpoint_and_handle() -> None:
    loaded, route = configuration()
    route_policy = bedrock_policy()

    authorized = authorize_bedrock_route(
        loaded,
        route,
        route_policy,
        identity(BedrockCredentialSourceKind.INSTANCE_METADATA),
    )

    assert authorized.region == "us-east-1"
    assert authorized.model_id == "amazon.nova-lite-v1:0"
    assert authorized.endpoint_url == ENDPOINT
    assert not authorized.cross_region_inference


def test_cross_region_endpoint_and_credential_substitution_fail_closed() -> None:
    with pytest.raises(ValidationError, match="endpoint"):
        bedrock_policy(
            endpoint_url="https://bedrock-runtime.eu-west-1.amazonaws.com/",
            destination_sha256=provider_destination_sha256(
                "https://bedrock-runtime.eu-west-1.amazonaws.com/"
            ),
        )
    with pytest.raises(ValidationError, match="regional"):
        bedrock_policy(
            allowed_inference_regions=("eu-west-1", "us-east-1"),
        )
    loaded, route = configuration()
    substituted = identity(BedrockCredentialSourceKind.INSTANCE_METADATA).model_copy(
        update={"credential_handle": "pcr_" + "9" * 32}
    )
    with pytest.raises(ValueError, match="authorization"):
        authorize_bedrock_route(
            loaded,
            route,
            bedrock_policy(),
            substituted,
        )


def identity(
    source: BedrockCredentialSourceKind,
    **updates: object,
) -> BedrockIdentityReference:
    values = {
        "identity_reference_id": IDENTITY_ID,
        "credential_handle": HANDLE,
        "source": source,
        **updates,
    }
    return BedrockIdentityReference.model_validate(values)


def bedrock_policy(**updates: object) -> BedrockRoutePolicy:
    values = {
        "route_id": "bedrock.primary",
        "model_id": "amazon.nova-lite-v1:0",
        "region": "us-east-1",
        "credential_handle": HANDLE,
        "identity_reference_id": IDENTITY_ID,
        "endpoint_url": ENDPOINT,
        "destination_sha256": DESTINATION_SHA256,
        "allowed_inference_regions": ("us-east-1",),
    }
    values.update(updates)
    return BedrockRoutePolicy.model_validate(values)


def configuration():
    provider_model = model().model_copy(
        update={
            "provider": "bedrock",
            "model": "amazon.nova-lite-v1:0",
        }
    )
    data_policy = policy().model_copy(
        update={
            "provider": "bedrock",
            "destination_sha256": DESTINATION_SHA256,
        }
    )
    provider_price = price().model_copy(
        update={
            "provider": "bedrock",
            "model": "amazon.nova-lite-v1:0",
        }
    )
    route = ProviderRouteConfiguration(
        route_id="bedrock.primary",
        provider="bedrock",
        model="amazon.nova-lite-v1:0",
        model_revision_sha256=provider_model.model_revision_sha256,
        region="us-east-1",
        credential_handle=HANDLE,
        policy_revision_sha256=data_policy.policy_revision_sha256,
        price_version_sha256=provider_price.price_version_sha256,
        priority=1,
        enabled=True,
    )
    configuration = ProviderConfiguration(
        schema_version=1,
        models=(provider_model,),
        credential_bindings=(
            ProviderCredentialBinding(
                handle=HANDLE,
                provider="bedrock",
                destination_sha256=DESTINATION_SHA256,
            ),
        ),
        data_policies=(data_policy,),
        prices=(provider_price,),
        routes=(route,),
    )
    return (
        LoadedProviderConfiguration(
            content_sha256="f" * 64,
            configuration=configuration,
        ),
        route,
    )
