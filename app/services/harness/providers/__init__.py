"""Capability-split Atlas Harness provider adapters."""

from app.services.harness.providers.config_contracts import (
    LoadedProviderConfiguration,
    ProviderConfiguration,
    ProviderCredentialBinding,
    ProviderRouteConfiguration,
)
from app.services.harness.providers.config_loader import (
    ProviderConfigLoadError,
    ProviderConfigLoadErrorCode,
    load_provider_configuration,
)
from app.services.harness.providers.configured_catalog import (
    ConfiguredCatalogError,
    ConfiguredCatalogErrorCode,
    ConfiguredModelCatalog,
)
from app.services.harness.providers.credential_broker import (
    ConfiguredCredentialBroker,
)
from app.services.harness.providers.credential_material import (
    CredentialBrokerError,
    CredentialBrokerErrorCode,
    CredentialLease,
    CredentialSecretBackend,
    CredentialSecretMaterial,
)
from app.services.harness.providers.environment_credentials import (
    EnvironmentCredentialBackend,
    EnvironmentCredentialError,
    EnvironmentCredentialErrorCode,
    EnvironmentCredentialReference,
)
from app.services.harness.providers.health_registry import (
    ProviderHealthRegistry,
    ProviderHealthRegistryError,
    ProviderHealthRegistryErrorCode,
)
from app.services.harness.providers.recorded import (
    RecordedProviderError,
    RecordedProviderErrorCode,
    RecordedProviderStream,
)
from app.services.harness.providers.route_inventory import (
    ConfiguredRouteInventory,
    ConfiguredRouteInventoryError,
    ConfiguredRouteInventoryErrorCode,
)

__all__ = (
    "ConfiguredCatalogError",
    "ConfiguredCatalogErrorCode",
    "ConfiguredModelCatalog",
    "ConfiguredCredentialBroker",
    "ConfiguredRouteInventory",
    "ConfiguredRouteInventoryError",
    "ConfiguredRouteInventoryErrorCode",
    "CredentialBrokerError",
    "CredentialBrokerErrorCode",
    "CredentialLease",
    "CredentialSecretBackend",
    "CredentialSecretMaterial",
    "EnvironmentCredentialBackend",
    "EnvironmentCredentialError",
    "EnvironmentCredentialErrorCode",
    "EnvironmentCredentialReference",
    "LoadedProviderConfiguration",
    "ProviderConfigLoadError",
    "ProviderConfigLoadErrorCode",
    "ProviderConfiguration",
    "ProviderCredentialBinding",
    "ProviderHealthRegistry",
    "ProviderHealthRegistryError",
    "ProviderHealthRegistryErrorCode",
    "RecordedProviderError",
    "RecordedProviderErrorCode",
    "RecordedProviderStream",
    "ProviderRouteConfiguration",
    "load_provider_configuration",
)
