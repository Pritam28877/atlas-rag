"""Side-effect-free admission for a six-call local capability probe."""

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


class LocalProbeGateErrorCode(StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"
    ENVIRONMENT = "environment"
    PATH = "path"
    TIMEOUT = "timeout"
    TTL = "ttl"


class LocalProbeGateError(ValueError):
    def __init__(self, code: LocalProbeGateErrorCode) -> None:
        super().__init__("local capability probe admission failed")
        self.code = code


class LocalProbeLaunchRequest(StrictProtocolModel):
    acknowledged: bool = False
    model: ModelName
    per_case_timeout_seconds: int | None = None
    evidence_ttl_seconds: int | None = None
    gate_environment_variable: str = Field(min_length=1, max_length=128)
    credential_environment_variable: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    configuration_path: Path
    route_policy_path: Path
    identity_path: Path
    result_path: Path


class AuthorizedLocalProbe(StrictProtocolModel):
    acknowledged: Literal[True]
    model: ModelName
    maximum_provider_calls: Literal[6] = 6
    per_case_timeout_seconds: int = Field(ge=1, le=30)
    evidence_ttl_seconds: int = Field(ge=1, le=3_600)
    gate_environment_variable: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]{0,127}$"
    )
    credential_environment_variable: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{0,127}$",
    )
    configuration_path: Path
    route_policy_path: Path
    identity_path: Path
    result_path: Path


def authorize_local_probe(
    request: LocalProbeLaunchRequest,
) -> AuthorizedLocalProbe:
    if not request.acknowledged:
        _reject(LocalProbeGateErrorCode.ACKNOWLEDGEMENT)
    if (
        request.per_case_timeout_seconds is None
        or not 1 <= request.per_case_timeout_seconds <= 30
    ):
        _reject(LocalProbeGateErrorCode.TIMEOUT)
    if (
        request.evidence_ttl_seconds is None
        or not 1 <= request.evidence_ttl_seconds <= 3_600
        or (
            request.per_case_timeout_seconds is not None
            and request.evidence_ttl_seconds
            <= request.per_case_timeout_seconds * 6
        )
    ):
        _reject(LocalProbeGateErrorCode.TTL)
    if not environment_name_is_safe(
        request.gate_environment_variable
    ) or (
        request.credential_environment_variable is not None
        and not environment_name_is_safe(
            request.credential_environment_variable
        )
    ):
        _reject(LocalProbeGateErrorCode.ENVIRONMENT)
    paths = (
        request.configuration_path,
        request.route_policy_path,
        request.identity_path,
        request.result_path,
    )
    if (
        any(not path.is_absolute() for path in paths)
        or len(paths) != len(set(paths))
    ):
        _reject(LocalProbeGateErrorCode.PATH)
    return AuthorizedLocalProbe(
        acknowledged=True,
        model=request.model,
        per_case_timeout_seconds=request.per_case_timeout_seconds,
        evidence_ttl_seconds=request.evidence_ttl_seconds,
        gate_environment_variable=request.gate_environment_variable,
        credential_environment_variable=(
            request.credential_environment_variable
        ),
        configuration_path=request.configuration_path,
        route_policy_path=request.route_policy_path,
        identity_path=request.identity_path,
        result_path=request.result_path,
    )


def _reject(code: LocalProbeGateErrorCode) -> Never:
    raise LocalProbeGateError(code)
