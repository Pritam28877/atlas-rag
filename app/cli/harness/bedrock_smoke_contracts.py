"""Signed, side-effect-free admission contracts for a Bedrock live smoke."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Never, Self

from pydantic import Field, model_validator

from app.cli.harness.provider_smoke_contracts import (
    environment_name_is_safe,
)
from app.services.harness.protocol import (
    Region,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.routing import ModelName
from app.services.harness.providers import BedrockIdentityReferenceId

MAXIMUM_BEDROCK_SMOKE_GRANT_SECONDS = 3_600
MINIMUM_BEDROCK_SMOKE_SIGNING_KEY_BYTES = 32
MAXIMUM_BEDROCK_SMOKE_SIGNING_KEY_BYTES = 4_096


class BedrockSmokeGateErrorCode(StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"
    BINDING = "binding"
    ENVIRONMENT = "environment"
    EXPIRY = "expiry"
    PATH = "path"
    SIGNATURE = "signature"


class BedrockSmokeGateError(ValueError):
    def __init__(self, code: BedrockSmokeGateErrorCode) -> None:
        super().__init__("Bedrock live smoke admission failed")
        self.code = code


class BedrockSmokeBinding(StrictProtocolModel):
    configuration_sha256: Sha256
    route_policy_sha256: Sha256
    identity_sha256: Sha256
    identity_reference_id: BedrockIdentityReferenceId
    aws_account_id: str = Field(pattern=r"^[0-9]{12}$")
    model_id: ModelName
    region: Region
    destination_sha256: Sha256


class BedrockSmokeGrantPayload(StrictProtocolModel):
    version: Literal[1] = 1
    authorization_id: str = Field(pattern=r"^awsg_[0-9a-f]{32}$")
    key_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    binding: BedrockSmokeBinding
    maximum_provider_calls: Literal[2] = 2
    text_max_output_tokens: int = Field(ge=1, le=4_096)
    tool_max_output_tokens: int = Field(ge=1, le=4_096)
    text_cost_cap_microusd: int = Field(ge=1, le=10_000_000_000)
    tool_cost_cap_microusd: int = Field(ge=1, le=10_000_000_000)
    total_cost_cap_microusd: int = Field(ge=2, le=10_000_000_000)
    issued_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_budget_and_lifetime(self) -> Self:
        if (
            self.text_cost_cap_microusd + self.tool_cost_cap_microusd
            != self.total_cost_cap_microusd
        ):
            raise ValueError("Bedrock smoke cost caps must total exactly")
        lifetime = self.expires_at - self.issued_at
        if not timedelta(0) < lifetime <= timedelta(
            seconds=MAXIMUM_BEDROCK_SMOKE_GRANT_SECONDS
        ):
            raise ValueError("Bedrock smoke grant lifetime is invalid")
        return self


class SignedBedrockSmokeGrant(StrictProtocolModel):
    payload: BedrockSmokeGrantPayload
    signature_sha256: Sha256


class BedrockSmokeLaunchRequest(StrictProtocolModel):
    acknowledged: bool = False
    grant_path: Path
    configuration_path: Path
    route_policy_path: Path
    identity_path: Path
    database_path: Path
    result_path: Path
    signing_key_environment_variable: str = Field(
        min_length=1,
        max_length=128,
    )


class AdmittedBedrockSmokeLaunch(StrictProtocolModel):
    acknowledged: Literal[True]
    grant_path: Path
    configuration_path: Path
    route_policy_path: Path
    identity_path: Path
    database_path: Path
    result_path: Path
    signing_key_environment_variable: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]{0,127}$",
    )


class AuthorizedBedrockSmoke(StrictProtocolModel):
    launch: AdmittedBedrockSmokeLaunch
    grant: BedrockSmokeGrantPayload


def admit_bedrock_smoke(
    request: BedrockSmokeLaunchRequest,
) -> AdmittedBedrockSmokeLaunch:
    if not request.acknowledged:
        _reject(BedrockSmokeGateErrorCode.ACKNOWLEDGEMENT)
    if not environment_name_is_safe(
        request.signing_key_environment_variable
    ):
        _reject(BedrockSmokeGateErrorCode.ENVIRONMENT)
    paths = (
        request.grant_path,
        request.configuration_path,
        request.route_policy_path,
        request.identity_path,
        request.database_path,
        request.result_path,
    )
    if (
        any(not path.is_absolute() for path in paths)
        or len(paths) != len(set(paths))
    ):
        _reject(BedrockSmokeGateErrorCode.PATH)
    return AdmittedBedrockSmokeLaunch(
        acknowledged=True,
        grant_path=request.grant_path,
        configuration_path=request.configuration_path,
        route_policy_path=request.route_policy_path,
        identity_path=request.identity_path,
        database_path=request.database_path,
        result_path=request.result_path,
        signing_key_environment_variable=(
            request.signing_key_environment_variable
        ),
    )


def sign_bedrock_smoke_grant(
    payload: BedrockSmokeGrantPayload,
    signing_key: bytes | bytearray,
) -> SignedBedrockSmokeGrant:
    _validate_signing_key(signing_key)
    signature = hmac.digest(
        signing_key,
        _canonical_grant_payload(payload),
        "sha256",
    )
    return SignedBedrockSmokeGrant(
        payload=payload,
        signature_sha256=signature.hex(),
    )


def verify_bedrock_smoke_grant(
    launch: AdmittedBedrockSmokeLaunch,
    signed_grant: SignedBedrockSmokeGrant,
    signing_key: bytes | bytearray,
    expected_binding: BedrockSmokeBinding,
    *,
    observed_at: datetime,
) -> AuthorizedBedrockSmoke:
    try:
        _validate_signing_key(signing_key)
    except BedrockSmokeGateError:
        _reject(BedrockSmokeGateErrorCode.SIGNATURE)
    expected_signature = hmac.digest(
        signing_key,
        _canonical_grant_payload(signed_grant.payload),
        "sha256",
    ).hex()
    if not hmac.compare_digest(
        signed_grant.signature_sha256,
        expected_signature,
    ):
        _reject(BedrockSmokeGateErrorCode.SIGNATURE)
    if (
        observed_at.tzinfo is None
        or observed_at.utcoffset() != timedelta(0)
        or not signed_grant.payload.issued_at
        <= observed_at
        < signed_grant.payload.expires_at
    ):
        _reject(BedrockSmokeGateErrorCode.EXPIRY)
    if signed_grant.payload.binding != expected_binding:
        _reject(BedrockSmokeGateErrorCode.BINDING)
    return AuthorizedBedrockSmoke(
        launch=launch,
        grant=signed_grant.payload,
    )


def bedrock_smoke_model_sha256(model: StrictProtocolModel) -> str:
    content = json.dumps(
        model.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _canonical_grant_payload(payload: BedrockSmokeGrantPayload) -> bytes:
    return json.dumps(
        payload.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _validate_signing_key(signing_key: bytes | bytearray) -> None:
    if (
        not isinstance(signing_key, (bytes, bytearray))
        or not MINIMUM_BEDROCK_SMOKE_SIGNING_KEY_BYTES
        <= len(signing_key)
        <= MAXIMUM_BEDROCK_SMOKE_SIGNING_KEY_BYTES
    ):
        _reject(BedrockSmokeGateErrorCode.SIGNATURE)


def _reject(code: BedrockSmokeGateErrorCode) -> Never:
    raise BedrockSmokeGateError(code)
