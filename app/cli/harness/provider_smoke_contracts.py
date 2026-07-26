"""Side-effect-free admission contracts for explicit live provider smokes."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers import canonical_provider_url


class ProviderSmokeGateErrorCode(StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"
    COST_CAP = "cost_cap"
    ENDPOINT = "endpoint"
    ENVIRONMENT = "environment"
    PATH = "path"
    POLICY = "policy"
    TIMEOUT = "timeout"
    TOKEN_CAP = "token_cap"


class ProviderSmokeGateError(ValueError):
    def __init__(self, code: ProviderSmokeGateErrorCode) -> None:
        super().__init__("live provider smoke admission failed")
        self.code = code


class ProviderSmokeLaunchRequest(StrictProtocolModel):
    acknowledged: bool = False
    provider: Literal["openai", "openrouter"]
    model: ModelName
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None
    cost_cap_microusd: int | None = None
    config_path: Path
    database_path: Path
    result_path: Path
    destination_url: str = Field(min_length=9, max_length=2_048)
    environment_variable: str = Field(min_length=1, max_length=128)
    openrouter_policy_path: Path | None = None


class AuthorizedProviderSmoke(StrictProtocolModel):
    acknowledged: Literal[True]
    provider: Literal["openai", "openrouter"]
    model: ModelName
    max_output_tokens: int = Field(ge=1, le=4_096)
    timeout_seconds: int = Field(ge=1, le=60)
    cost_cap_microusd: int = Field(ge=1, le=10_000_000_000)
    config_path: Path
    database_path: Path
    result_path: Path
    destination_url: str = Field(min_length=9, max_length=2_048)
    environment_variable: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]{0,127}$",
    )
    openrouter_policy_path: Path | None = None


def authorize_provider_smoke(
    request: ProviderSmokeLaunchRequest,
) -> AuthorizedProviderSmoke:
    if not request.acknowledged:
        raise ProviderSmokeGateError(
            ProviderSmokeGateErrorCode.ACKNOWLEDGEMENT
        )
    if (
        request.max_output_tokens is None
        or not 1 <= request.max_output_tokens <= 4_096
    ):
        raise ProviderSmokeGateError(ProviderSmokeGateErrorCode.TOKEN_CAP)
    if (
        request.timeout_seconds is None
        or not 1 <= request.timeout_seconds <= 60
    ):
        raise ProviderSmokeGateError(ProviderSmokeGateErrorCode.TIMEOUT)
    if (
        request.cost_cap_microusd is None
        or not 1 <= request.cost_cap_microusd <= 10_000_000_000
    ):
        raise ProviderSmokeGateError(ProviderSmokeGateErrorCode.COST_CAP)
    if not _environment_name_is_safe(request.environment_variable):
        raise ProviderSmokeGateError(
            ProviderSmokeGateErrorCode.ENVIRONMENT
        )
    try:
        destination_url = canonical_provider_url(request.destination_url)
    except ValueError:
        raise ProviderSmokeGateError(
            ProviderSmokeGateErrorCode.ENDPOINT
        ) from None
    paths = (
        request.config_path,
        request.database_path,
        request.result_path,
    )
    if (
        any(not path.is_absolute() for path in paths)
        or len(set(paths)) != len(paths)
    ):
        raise ProviderSmokeGateError(ProviderSmokeGateErrorCode.PATH)
    if request.openrouter_policy_path is not None:
        if not request.openrouter_policy_path.is_absolute():
            raise ProviderSmokeGateError(ProviderSmokeGateErrorCode.PATH)
        if request.openrouter_policy_path in paths:
            raise ProviderSmokeGateError(ProviderSmokeGateErrorCode.PATH)
    uses_openrouter = request.provider == "openrouter"
    if uses_openrouter != (request.openrouter_policy_path is not None):
        raise ProviderSmokeGateError(ProviderSmokeGateErrorCode.POLICY)
    return AuthorizedProviderSmoke(
        acknowledged=True,
        provider=request.provider,
        model=request.model,
        max_output_tokens=request.max_output_tokens,
        timeout_seconds=request.timeout_seconds,
        cost_cap_microusd=request.cost_cap_microusd,
        config_path=request.config_path,
        database_path=request.database_path,
        result_path=request.result_path,
        destination_url=destination_url,
        environment_variable=request.environment_variable,
        openrouter_policy_path=request.openrouter_policy_path,
    )


def _environment_name_is_safe(value: str) -> bool:
    return (
        bool(value)
        and value[0].isalpha()
        and value[0].isupper()
        and all(
            character.isascii()
            and (
                character.isupper()
            or character.isdigit()
            or character == "_"
            )
            for character in value
        )
    )
