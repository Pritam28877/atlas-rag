import hashlib
from datetime import UTC, datetime, timedelta

from app.services.harness.protocol import ProviderContextPlan
from app.services.harness.providers import (
    LoadedProviderConfiguration,
    ProviderConfiguration,
    ProviderCredentialBinding,
    ProviderRouteConfiguration,
)
from app.services.harness.providers.local_compatible_capabilities import (
    LocalCompatibleFeature,
    LocalCompatibleProbe,
    decide_local_compatible_request,
)
from app.services.harness.providers.local_compatible_identity import (
    LocalAuthenticationMode,
    LocalCompatibleIdentityReference,
)
from app.services.harness.providers.local_compatible_policy import (
    LocalCompatibleRoutePolicy,
    LocalEndpointMode,
    authorize_local_compatible_route,
    local_destination_sha256,
)
from tests.harness_provider_capability_fixtures import (
    model,
    policy,
    price,
    request,
)

ENDPOINT = "http://127.0.0.1:11434/v1/"
DESTINATION_SHA256 = local_destination_sha256(ENDPOINT)
HANDLE = "pcr_" + "1" * 32
IDENTITY_ID = "locid_" + "2" * 32
MODEL_ID = "configured-local-model"
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
BASE_FEATURES = (
    LocalCompatibleFeature.MODALITY_TEXT,
    LocalCompatibleFeature.RESPONSES_STREAM,
    LocalCompatibleFeature.STORE_FALSE,
    LocalCompatibleFeature.STREAM_CANCEL,
    LocalCompatibleFeature.STREAM_USAGE,
    LocalCompatibleFeature.TRUNCATION_DISABLED,
)


def identity(
    authentication: LocalAuthenticationMode = LocalAuthenticationMode.NONE,
    **updates: object,
) -> LocalCompatibleIdentityReference:
    values = {
        "identity_reference_id": IDENTITY_ID,
        "credential_handle": HANDLE,
        "authentication": authentication,
        **updates,
    }
    return LocalCompatibleIdentityReference.model_validate(values)


def route_policy(**updates: object) -> LocalCompatibleRoutePolicy:
    values = {
        "route_id": "local.primary",
        "model_id": MODEL_ID,
        "region": "local",
        "credential_handle": HANDLE,
        "identity_reference_id": IDENTITY_ID,
        "endpoint_mode": LocalEndpointMode.LOOPBACK,
        "endpoint_url": ENDPOINT,
        "destination_sha256": DESTINATION_SHA256,
        "allowed_remote_hostnames": (),
    }
    values.update(updates)
    return LocalCompatibleRoutePolicy.model_validate(values)


def configuration() -> tuple[
    LoadedProviderConfiguration,
    ProviderRouteConfiguration,
]:
    provider_model = model().model_copy(
        update={
            "provider": "local-compatible",
            "model": MODEL_ID,
            "available_regions": ("local",),
        }
    )
    data_policy = policy().model_copy(
        update={
            "provider": "local-compatible",
            "destination_sha256": DESTINATION_SHA256,
            "allowed_regions": ("local",),
        }
    )
    provider_price = price().model_copy(
        update={
            "provider": "local-compatible",
            "model": MODEL_ID,
            "region": "local",
        }
    )
    route = ProviderRouteConfiguration(
        route_id="local.primary",
        provider="local-compatible",
        model=MODEL_ID,
        model_revision_sha256=provider_model.model_revision_sha256,
        region="local",
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
                provider="local-compatible",
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
            configuration=provider_configuration,
        ),
        route,
    )


def local_request():
    return request().model_copy(
        update={
            "route_id": "local.primary",
            "deadline_at": NOW + timedelta(minutes=5),
        }
    )


def local_model():
    return configuration()[0].configuration.models[0]


def context(canonical_request=None) -> ProviderContextPlan:
    selected = canonical_request or local_request()
    return ProviderContextPlan(
        provider_request_sha256=hashlib.sha256(
            selected.model_dump_json().encode()
        ).hexdigest(),
        model_revision_sha256=local_model().model_revision_sha256,
        applied_features=(),
        estimated_input_tokens=10,
        reason="Local capability decision context.",
    )


def probe(
    features: tuple[LocalCompatibleFeature, ...] = BASE_FEATURES,
    **updates: object,
) -> LocalCompatibleProbe:
    values = {
        "route_id": "local.primary",
        "model_id": MODEL_ID,
        "model_revision_sha256": local_model().model_revision_sha256,
        "destination_sha256": DESTINATION_SHA256,
        "supported_features": features,
        "evidence_sha256": "a" * 64,
        "observed_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
    }
    values.update(updates)
    return LocalCompatibleProbe.model_validate(values)


def authorized_route():
    loaded, route = configuration()
    return authorize_local_compatible_route(
        loaded,
        route,
        route_policy(),
        identity(),
    )


def accepted_decision():
    return decide_local_compatible_request(
        local_request(),
        context(),
        authorized_route(),
        probe(),
        decided_at=NOW + timedelta(minutes=1),
    )
