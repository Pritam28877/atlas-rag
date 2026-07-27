"""Private input loading and redacted output for the Bedrock smoke."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from app.cli.harness.bedrock_smoke_contracts import (
    AdmittedBedrockSmokeLaunch,
    SignedBedrockSmokeGrant,
)
from app.cli.harness.provider_smoke_io import (
    read_private_smoke_input,
    write_private_smoke_output,
)
from app.cli.harness.smoke_timing import MAXIMUM_SMOKE_LATENCY_MS
from app.services.harness.protocol import (
    ModelName,
    Region,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.providers import (
    BedrockIdentityReference,
    BedrockRoutePolicy,
)

MAXIMUM_BEDROCK_SMOKE_INPUT_BYTES = 64 * 1024


class BedrockSmokePrivateInputs(StrictProtocolModel):
    signed_grant: SignedBedrockSmokeGrant
    route_policy: BedrockRoutePolicy
    identity: BedrockIdentityReference


class BedrockSmokeResult(StrictProtocolModel):
    provider: Literal["bedrock"] = "bedrock"
    model: ModelName
    region: Region
    authorization_id_sha256: Sha256
    destination_sha256: Sha256
    text_request_sha256: Sha256
    tool_request_sha256: Sha256
    text_provider_metadata_sha256: Sha256 | None = None
    tool_provider_metadata_sha256: Sha256 | None = None
    input_tokens: int = Field(ge=0, le=4_000_000)
    cached_input_tokens: int = Field(ge=0, le=4_000_000)
    output_tokens: int = Field(ge=0, le=1_024_000)
    reasoning_tokens: int = Field(ge=0, le=1_024_000)
    latency_ms: int = Field(ge=0, le=MAXIMUM_SMOKE_LATENCY_MS)
    cancellation_latency_ms: int = Field(ge=0, le=60_000)
    charged_cost_microusd: int = Field(ge=0, le=10_000_000_000)
    signed_cost_cap_microusd: int = Field(ge=2, le=10_000_000_000)
    provider_calls: Literal[2] = 2
    text_verified: Literal[True] = True
    tool_verified: Literal[True] = True
    cancellation_verified: Literal[True] = True
    active_cost_reservations: Literal[0] = 0
    completed_at: UtcTimestamp


async def load_bedrock_smoke_private_inputs(
    launch: AdmittedBedrockSmokeLaunch,
) -> BedrockSmokePrivateInputs:
    contents = await asyncio.gather(
        read_private_smoke_input(
            launch.grant_path,
            maximum_bytes=MAXIMUM_BEDROCK_SMOKE_INPUT_BYTES,
        ),
        read_private_smoke_input(
            launch.route_policy_path,
            maximum_bytes=MAXIMUM_BEDROCK_SMOKE_INPUT_BYTES,
        ),
        read_private_smoke_input(
            launch.identity_path,
            maximum_bytes=MAXIMUM_BEDROCK_SMOKE_INPUT_BYTES,
        ),
    )
    try:
        return BedrockSmokePrivateInputs(
            signed_grant=SignedBedrockSmokeGrant.model_validate_json(
                contents[0]
            ),
            route_policy=BedrockRoutePolicy.model_validate_json(contents[1]),
            identity=BedrockIdentityReference.model_validate_json(contents[2]),
        )
    except ValidationError:
        raise ValueError("Bedrock smoke private input is invalid") from None


def load_bedrock_smoke_signing_key(
    variable_name: str,
    environment: Mapping[bytes, bytes] | None,
) -> bytearray:
    source = os.environb if environment is None else environment
    value = source.get(variable_name.encode())
    if value is None:
        raise ValueError("Bedrock smoke signing key is unavailable")
    return bytearray(value)


async def write_bedrock_smoke_result(
    path: Path,
    result: BedrockSmokeResult,
) -> None:
    await write_private_smoke_output(
        path,
        result.model_dump_json().encode(),
    )
