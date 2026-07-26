"""Multi-provider configuration for bounded catalog pagination tests."""

from app.services.harness.protocol import (
    ProviderDataPolicy,
    ProviderModelCapabilities,
    ProviderPriceRecord,
)
from app.services.harness.providers import (
    LoadedProviderConfiguration,
    ProviderConfiguration,
    ProviderCredentialBinding,
    ProviderRouteConfiguration,
)
from tests.harness_provider_capability_fixtures import model, policy, price


def configured_catalog_fixture(
    *,
    primary_snapshot_sha256: str = "4" * 64,
) -> LoadedProviderConfiguration:
    primary_model_values = model().model_dump(mode="python")
    primary_model_values["catalog_snapshot_sha256"] = (
        primary_snapshot_sha256
    )
    primary_model = ProviderModelCapabilities.model_validate(
        primary_model_values
    )
    second_model_values = dict(primary_model_values)
    second_model_values.update(
        {
            "model": "configured-model-v2",
            "model_revision_sha256": "a" * 64,
        }
    )
    second_model = ProviderModelCapabilities.model_validate(
        second_model_values
    )
    external_model_values = dict(primary_model_values)
    external_model_values.update(
        {
            "provider": "secondary-provider",
            "model": "secondary-model",
            "model_revision_sha256": "c" * 64,
            "catalog_snapshot_sha256": "d" * 64,
            "available_regions": ("eu-west-1",),
        }
    )
    external_model = ProviderModelCapabilities.model_validate(
        external_model_values
    )

    external_policy_values = policy().model_dump(mode="python")
    external_policy_values.update(
        {
            "provider": "secondary-provider",
            "policy_revision_sha256": "e" * 64,
            "destination_sha256": "f" * 64,
            "allowed_regions": ("eu-west-1",),
        }
    )
    external_policy = ProviderDataPolicy.model_validate(
        external_policy_values
    )

    second_price_values = price().model_dump(mode="python")
    second_price_values.update(
        {
            "model": "configured-model-v2",
            "model_revision_sha256": "a" * 64,
            "price_version_sha256": "b" * 64,
        }
    )
    second_price = ProviderPriceRecord.model_validate(second_price_values)
    external_price_values = price().model_dump(mode="python")
    external_price_values.update(
        {
            "provider": "secondary-provider",
            "model": "secondary-model",
            "model_revision_sha256": "c" * 64,
            "region": "eu-west-1",
            "price_version_sha256": "c" * 64,
        }
    )
    external_price = ProviderPriceRecord.model_validate(
        external_price_values
    )

    configuration = ProviderConfiguration(
        schema_version=1,
        models=(primary_model, second_model, external_model),
        credential_bindings=(
            ProviderCredentialBinding(
                handle="pcr_" + "9" * 32,
                provider="configured-provider",
                destination_sha256="6" * 64,
            ),
            ProviderCredentialBinding(
                handle="pcr_" + "a" * 32,
                provider="secondary-provider",
                destination_sha256="f" * 64,
            ),
        ),
        data_policies=(policy(), external_policy),
        prices=(price(), second_price, external_price),
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
            ProviderRouteConfiguration(
                route_id="configured.secondary",
                provider="configured-provider",
                model="configured-model-v2",
                model_revision_sha256="a" * 64,
                region="us-east-1",
                credential_handle="pcr_" + "9" * 32,
                policy_revision_sha256="5" * 64,
                price_version_sha256="b" * 64,
                priority=200,
                enabled=True,
            ),
            ProviderRouteConfiguration(
                route_id="secondary.primary",
                provider="secondary-provider",
                model="secondary-model",
                model_revision_sha256="c" * 64,
                region="eu-west-1",
                credential_handle="pcr_" + "a" * 32,
                policy_revision_sha256="e" * 64,
                price_version_sha256="c" * 64,
                priority=300,
                enabled=True,
            ),
        ),
    )
    return LoadedProviderConfiguration(
        content_sha256="f" * 64,
        configuration=configuration,
    )
