"""Atlas Harness CLI and local app-server composition boundary."""

from app.cli.harness.adapter_smoke_contracts import (
    AdapterSmokeGateError,
    AdapterSmokeGateErrorCode,
    AdapterSmokeLaunchRequest,
    AuthorizedAdapterSmoke,
    authorize_adapter_smoke,
)
from app.cli.harness.adapter_smoke_io import (
    AdapterSmokeResult,
    build_adapter_smoke_result,
    write_adapter_smoke_result,
)
from app.cli.harness.app_server import (
    AtlasLocalAppServer,
    HarnessConnectionFactory,
    LocalAppServerSnapshot,
)
from app.cli.harness.bedrock_smoke_contracts import (
    AdmittedBedrockSmokeLaunch,
    AuthorizedBedrockSmoke,
    BedrockSmokeBinding,
    BedrockSmokeGateError,
    BedrockSmokeGateErrorCode,
    BedrockSmokeGrantPayload,
    BedrockSmokeLaunchRequest,
    SignedBedrockSmokeGrant,
    admit_bedrock_smoke,
    bedrock_smoke_model_sha256,
    sign_bedrock_smoke_grant,
    verify_bedrock_smoke_grant,
)
from app.cli.harness.bedrock_smoke_io import BedrockSmokeResult
from app.cli.harness.bedrock_smoke_runner import run_bedrock_smoke
from app.cli.harness.local_adapter_smoke_runner import (
    run_local_adapter_smoke,
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
    "AdmittedBedrockSmokeLaunch",
    "AdapterSmokeGateError",
    "AdapterSmokeGateErrorCode",
    "AdapterSmokeLaunchRequest",
    "AdapterSmokeResult",
    "AtlasLocalAppServer",
    "AuthorizedAdapterSmoke",
    "AuthorizedProviderSmoke",
    "AuthorizedBedrockSmoke",
    "BedrockSmokeBinding",
    "BedrockSmokeGateError",
    "BedrockSmokeGateErrorCode",
    "BedrockSmokeGrantPayload",
    "BedrockSmokeLaunchRequest",
    "BedrockSmokeResult",
    "HarnessConnectionFactory",
    "LocalAppServerSnapshot",
    "ProviderSmokeGateError",
    "ProviderSmokeGateErrorCode",
    "ProviderSmokeLaunchRequest",
    "ProviderSmokeResult",
    "SignedBedrockSmokeGrant",
    "admit_bedrock_smoke",
    "authorize_adapter_smoke",
    "build_adapter_smoke_result",
    "authorize_provider_smoke",
    "bedrock_smoke_model_sha256",
    "run_provider_smoke",
    "run_bedrock_smoke",
    "run_local_adapter_smoke",
    "sign_bedrock_smoke_grant",
    "verify_bedrock_smoke_grant",
    "write_adapter_smoke_result",
)
