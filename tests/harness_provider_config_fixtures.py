"""Valid secret-free provider configuration used by loader tests."""

from app.services.harness.providers import (
    ProviderConfiguration,
    ProviderCredentialBinding,
    ProviderRouteConfiguration,
)
from tests.harness_provider_capability_fixtures import model, policy, price


def provider_configuration() -> ProviderConfiguration:
    return ProviderConfiguration(
        schema_version=1,
        models=(model(),),
        credential_bindings=(
            ProviderCredentialBinding(
                handle="pcr_" + "9" * 32,
                provider="configured-provider",
                destination_sha256="6" * 64,
            ),
        ),
        data_policies=(policy(),),
        prices=(price(),),
        routes=(
            ProviderRouteConfiguration(
                route_id="configured.primary",
                provider="configured-provider",
                model="configured-model",
                model_revision_sha256="3" * 64,
                region="us-east-1",
                credential_handle="pcr_" + "9" * 32,
                policy_revision_sha256="5" * 64,
                price_version_sha256="7" * 64,
                priority=100,
                enabled=True,
            ),
        ),
    )
