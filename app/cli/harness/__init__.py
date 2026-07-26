"""Atlas Harness CLI and local app-server composition boundary."""

from app.cli.harness.app_server import (
    AtlasLocalAppServer,
    HarnessConnectionFactory,
    LocalAppServerSnapshot,
)
from app.cli.harness.provider_smoke_contracts import (
    AuthorizedProviderSmoke,
    ProviderSmokeGateError,
    ProviderSmokeGateErrorCode,
    ProviderSmokeLaunchRequest,
    authorize_provider_smoke,
)
from app.cli.harness.provider_smoke_io import ProviderSmokeResult
from app.cli.harness.provider_smoke_runner import run_provider_smoke

__all__ = (
    "AtlasLocalAppServer",
    "AuthorizedProviderSmoke",
    "HarnessConnectionFactory",
    "LocalAppServerSnapshot",
    "ProviderSmokeGateError",
    "ProviderSmokeGateErrorCode",
    "ProviderSmokeLaunchRequest",
    "ProviderSmokeResult",
    "authorize_provider_smoke",
    "run_provider_smoke",
)
