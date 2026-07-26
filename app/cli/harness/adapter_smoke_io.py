"""Redacted canonical evidence for Vertex and local-compatible smokes."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.cli.harness.provider_smoke_decode import EXPECTED_SMOKE_RESPONSE
from app.cli.harness.provider_smoke_io import write_private_smoke_output
from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderFinishReason,
    ProviderName,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderUsage,
    RequestId,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.routing import ModelName

MAXIMUM_ADAPTER_SMOKE_EVENTS = 256
MAXIMUM_ADAPTER_SMOKE_OUTPUT_BYTES = 4 * 1024


class AdapterSmokeResult(StrictProtocolModel):
    provider: ProviderName
    model: ModelName
    request_id: RequestId
    maximum_provider_calls: Literal[1] = 1
    canonical_shape: Literal["text_usage_completed"] = (
        "text_usage_completed"
    )
    event_count: int = Field(ge=3, le=MAXIMUM_ADAPTER_SMOKE_EVENTS)
    output_sha256: Sha256
    canonical_events_sha256: Sha256
    route_binding_sha256: Sha256
    input_tokens: int = Field(ge=0, le=2_000_000)
    cached_input_tokens: int = Field(ge=0, le=2_000_000)
    output_tokens: int = Field(ge=0, le=512_000)
    reasoning_tokens: int = Field(ge=0, le=512_000)
    charged_cost_microusd: int = Field(ge=0, le=1_000_000)
    output_verified: Literal[True] = True
    active_credential_leases: Literal[0] = 0
    completed_at: UtcTimestamp


def build_adapter_smoke_result(
    *,
    provider: ProviderName,
    model: ModelName,
    request_id: RequestId,
    events: Sequence[ProviderStreamEvent],
    route_binding_sha256: Sha256,
    charged_cost_microusd: int,
    completed_at: datetime,
) -> AdapterSmokeResult:
    if not 3 <= len(events) <= MAXIMUM_ADAPTER_SMOKE_EVENTS:
        raise ValueError("adapter smoke event count is invalid")
    if (
        not all(
            isinstance(event, ProviderTextDelta)
            for event in events[:-2]
        )
        or not isinstance(events[-2], ProviderUsage)
        or not isinstance(events[-1], ProviderCompleted)
        or tuple(event.sequence for event in events)
        != tuple(range(1, len(events) + 1))
    ):
        raise ValueError("adapter smoke canonical shape is invalid")
    text_parts: list[str] = []
    usage: ProviderTokenUsage | None = None
    completed = False
    digest = hashlib.sha256()
    for event in events:
        digest.update(
            hashlib.sha256(event.model_dump_json().encode()).digest()
        )
        if isinstance(event, ProviderTextDelta):
            text_parts.append(event.text)
        elif isinstance(event, ProviderUsage):
            if usage is not None:
                raise ValueError("adapter smoke emitted duplicate usage")
            usage = event.usage
        elif isinstance(event, ProviderCompleted):
            if (
                completed
                or event.finish_reason is not ProviderFinishReason.STOP
            ):
                raise ValueError("adapter smoke completion is invalid")
            completed = True
        else:
            raise ValueError("adapter smoke emitted an unexpected event")
    output = "".join(text_parts).encode()
    if (
        not 1 <= len(output) <= MAXIMUM_ADAPTER_SMOKE_OUTPUT_BYTES
        or output.decode() != EXPECTED_SMOKE_RESPONSE
        or usage is None
        or not completed
        or not isinstance(events[-1], ProviderCompleted)
    ):
        raise ValueError("adapter smoke evidence is incomplete")
    return AdapterSmokeResult(
        provider=provider,
        model=model,
        request_id=request_id,
        event_count=len(events),
        output_sha256=hashlib.sha256(output).hexdigest(),
        canonical_events_sha256=digest.hexdigest(),
        route_binding_sha256=route_binding_sha256,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        charged_cost_microusd=charged_cost_microusd,
        completed_at=completed_at,
    )


async def write_adapter_smoke_result(
    path: Path,
    result: AdapterSmokeResult,
) -> None:
    await write_private_smoke_output(
        path,
        result.model_dump_json().encode(),
    )
