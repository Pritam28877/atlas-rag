"""Side-effect-free admission for Vertex and local-compatible live smokes."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal, Never

from pydantic import Field

from app.cli.harness.provider_smoke_contracts import (
    environment_name_is_safe,
)
from app.services.harness.protocol import StrictProtocolModel
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers.vertex_identity import GcpProjectId

type AdapterSmokeProvider = Literal["local-compatible", "vertex"]


class AdapterSmokeGateErrorCode(StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"
    BUDGET = "budget"
    ENVIRONMENT = "environment"
    PATH = "path"
    PROVIDER = "provider"
    TIMEOUT = "timeout"
    TOKEN_CAP = "token_cap"


class AdapterSmokeGateError(ValueError):
    def __init__(self, code: AdapterSmokeGateErrorCode) -> None:
        super().__init__("adapter live smoke admission failed")
        self.code = code


class AdapterSmokeLaunchRequest(StrictProtocolModel):
    acknowledged: bool = False
    provider: AdapterSmokeProvider
    model: ModelName
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None
    cost_cap_microusd: int | None = None
    disposable_project_id: GcpProjectId | None = None
    gate_environment_variable: str = Field(min_length=1, max_length=128)
    credential_environment_variable: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    configuration_path: Path
    route_policy_path: Path
    identity_path: Path
    capability_evidence_path: Path | None = None
    database_path: Path
    result_path: Path


class AuthorizedAdapterSmoke(StrictProtocolModel):
    acknowledged: Literal[True]
    provider: AdapterSmokeProvider
    model: ModelName
    maximum_provider_calls: Literal[1] = 1
    max_output_tokens: int = Field(ge=1, le=256)
    timeout_seconds: int = Field(ge=1, le=30)
    cost_cap_microusd: int = Field(ge=0, le=1_000_000)
    disposable_project_id: GcpProjectId | None = None
    gate_environment_variable: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]{0,127}$",
    )
    credential_environment_variable: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]{0,127}$",
    )
    configuration_path: Path
    route_policy_path: Path
    identity_path: Path
    capability_evidence_path: Path | None = None
    database_path: Path
    result_path: Path


def authorize_adapter_smoke(
    request: AdapterSmokeLaunchRequest,
) -> AuthorizedAdapterSmoke:
    if not request.acknowledged:
        _reject(AdapterSmokeGateErrorCode.ACKNOWLEDGEMENT)
    if (
        request.max_output_tokens is None
        or not 1 <= request.max_output_tokens <= 256
    ):
        _reject(AdapterSmokeGateErrorCode.TOKEN_CAP)
    if (
        request.timeout_seconds is None
        or not 1 <= request.timeout_seconds <= 30
    ):
        _reject(AdapterSmokeGateErrorCode.TIMEOUT)
    if request.cost_cap_microusd is None:
        _reject(AdapterSmokeGateErrorCode.BUDGET)
    if not environment_name_is_safe(
        request.gate_environment_variable
    ):
        _reject(AdapterSmokeGateErrorCode.ENVIRONMENT)
    if (
        request.credential_environment_variable is not None
        and not environment_name_is_safe(
            request.credential_environment_variable
        )
    ):
        _reject(AdapterSmokeGateErrorCode.ENVIRONMENT)
    _validate_provider_binding(request)
    paths = (
        request.configuration_path,
        request.route_policy_path,
        request.identity_path,
        *(
            (request.capability_evidence_path,)
            if request.capability_evidence_path is not None
            else ()
        ),
        request.database_path,
        request.result_path,
    )
    if (
        any(not path.is_absolute() for path in paths)
        or len(paths) != len(set(paths))
    ):
        _reject(AdapterSmokeGateErrorCode.PATH)
    return AuthorizedAdapterSmoke(
        acknowledged=True,
        provider=request.provider,
        model=request.model,
        max_output_tokens=request.max_output_tokens,
        timeout_seconds=request.timeout_seconds,
        cost_cap_microusd=request.cost_cap_microusd,
        disposable_project_id=request.disposable_project_id,
        gate_environment_variable=request.gate_environment_variable,
        credential_environment_variable=(
            request.credential_environment_variable
        ),
        configuration_path=request.configuration_path,
        route_policy_path=request.route_policy_path,
        identity_path=request.identity_path,
        capability_evidence_path=request.capability_evidence_path,
        database_path=request.database_path,
        result_path=request.result_path,
    )


def _validate_provider_binding(
    request: AdapterSmokeLaunchRequest,
) -> None:
    if request.provider == "vertex":
        valid = (
            request.disposable_project_id is not None
            and request.credential_environment_variable is None
            and request.capability_evidence_path is None
            and request.cost_cap_microusd is not None
            and 1 <= request.cost_cap_microusd <= 1_000_000
        )
    else:
        valid = (
            request.disposable_project_id is None
            and request.capability_evidence_path is not None
            and request.cost_cap_microusd == 0
        )
    if not valid:
        _reject(AdapterSmokeGateErrorCode.PROVIDER)


def _reject(code: AdapterSmokeGateErrorCode) -> Never:
    raise AdapterSmokeGateError(code)
