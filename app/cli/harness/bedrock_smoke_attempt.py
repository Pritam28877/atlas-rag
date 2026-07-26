"""One cost-accounted Bedrock smoke attempt with bounded evidence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Never, Protocol

from app.cli.harness.bedrock_smoke_requests import (
    BEDROCK_SMOKE_EXPECTED_TEXT,
    BEDROCK_SMOKE_TOOL_ARGUMENTS,
    BEDROCK_SMOKE_TOOL_NAME,
)
from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderError,
    ProviderFinishReason,
    ProviderPriceRecord,
    ProviderReasoningDelta,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers import (
    AuthorizedBedrockRoute,
    BedrockConverseStreamDecoder,
    BedrockCredentialMaterial,
    CompiledBedrockConverseStreamRequest,
    ProviderAttemptSuccess,
)
from app.services.harness.providers.bedrock_contracts import (
    BedrockStreamMetadata,
)
from app.services.harness.providers.bedrock_identity import (
    BedrockIdentityReference,
)

TOKENS_PER_MILLION = 1_000_000


class BedrockSmokeKind(StrEnum):
    TEXT = "text"
    TOOL = "tool"


class BedrockSmokeCredentialResolver(Protocol):
    async def resolve(
        self,
        identity: BedrockIdentityReference,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> BedrockCredentialMaterial: ...

    async def close(self) -> None: ...


class BedrockSmokeTransport(Protocol):
    def stream(
        self,
        request: CompiledBedrockConverseStreamRequest,
        route: AuthorizedBedrockRoute,
        credential: BedrockCredentialMaterial,
        decoder: BedrockConverseStreamDecoder,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncIterator[ProviderStreamEvent]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BedrockSmokeCallEvidence:
    usage: ProviderTokenUsage
    metadata: BedrockStreamMetadata


class BedrockSmokeAttemptExecutor:
    def __init__(
        self,
        request: CompiledBedrockConverseStreamRequest,
        route: AuthorizedBedrockRoute,
        identity: BedrockIdentityReference,
        price: ProviderPriceRecord,
        kind: BedrockSmokeKind,
        resolver: BedrockSmokeCredentialResolver,
        transport: BedrockSmokeTransport,
    ) -> None:
        self._request = request
        self._route = route
        self._identity = identity
        self._price = price
        self._kind = kind
        self._resolver = resolver
        self._transport = transport

    async def execute(
        self,
        attempt: int,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> ProviderAttemptSuccess[BedrockSmokeCallEvidence]:
        if attempt != 1:
            raise ValueError("Bedrock smoke permits one attempt per call")
        credential = await self._resolver.resolve(
            self._identity,
            cancellation=cancellation,
            deadline_at=deadline_at,
        )
        decoder = BedrockConverseStreamDecoder(
            lambda input_tokens, cached_tokens, output_tokens, reasoning_tokens: (
                calculate_bedrock_cost_microusd(
                    self._price,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    reasoning_tokens,
                )
            )
        )
        events = self._transport.stream(
            self._request,
            self._route,
            credential,
            decoder,
            cancellation=cancellation,
            deadline_at=deadline_at,
        )
        evidence = await _verify_call(events, decoder, self._kind)
        return ProviderAttemptSuccess(
            value=evidence,
            actual_cost_microusd=evidence.usage.cost_microusd,
        )


def calculate_bedrock_cost_microusd(
    price: ProviderPriceRecord,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> int:
    uncached_input_tokens = input_tokens - cached_input_tokens
    if min(
        uncached_input_tokens,
        cached_input_tokens,
        output_tokens,
        reasoning_tokens,
    ) < 0:
        raise ValueError("Bedrock smoke usage is invalid")
    numerator = (
        uncached_input_tokens * price.input_microusd_per_million_tokens
        + cached_input_tokens
        * price.cached_input_microusd_per_million_tokens
        + output_tokens * price.output_microusd_per_million_tokens
        + reasoning_tokens
        * price.reasoning_microusd_per_million_tokens
    )
    return (numerator + TOKENS_PER_MILLION - 1) // TOKENS_PER_MILLION


async def _verify_call(
    events: AsyncIterator[ProviderStreamEvent],
    decoder: BedrockConverseStreamDecoder,
    kind: BedrockSmokeKind,
) -> BedrockSmokeCallEvidence:
    text = ""
    tool_call: ProviderToolCall | None = None
    usage: ProviderTokenUsage | None = None
    finish_reason: ProviderFinishReason | None = None
    async for event in events:
        if isinstance(event, ProviderTextDelta):
            text += event.text
            if (
                kind is BedrockSmokeKind.TOOL
                or not BEDROCK_SMOKE_EXPECTED_TEXT.startswith(text)
            ):
                _invalid_evidence()
        elif isinstance(event, ProviderToolCall):
            if tool_call is not None:
                _invalid_evidence()
            tool_call = event
        elif isinstance(event, ProviderUsage):
            if usage is not None:
                _invalid_evidence()
            usage = event.usage
        elif isinstance(event, ProviderCompleted):
            if finish_reason is not None:
                _invalid_evidence()
            finish_reason = event.finish_reason
        elif isinstance(event, ProviderError):
            _invalid_evidence()
        elif not isinstance(event, ProviderReasoningDelta):
            _invalid_evidence()
    metadata = decoder.metadata
    if usage is None or metadata is None:
        _invalid_evidence()
    if kind is BedrockSmokeKind.TEXT:
        valid = (
            text == BEDROCK_SMOKE_EXPECTED_TEXT
            and tool_call is None
            and finish_reason is ProviderFinishReason.STOP
        )
    else:
        valid = (
            not text
            and tool_call is not None
            and tool_call.tool_name == BEDROCK_SMOKE_TOOL_NAME
            and tool_call.arguments_json == BEDROCK_SMOKE_TOOL_ARGUMENTS
            and finish_reason is ProviderFinishReason.TOOL_CALLS
        )
    if not valid:
        _invalid_evidence()
    return BedrockSmokeCallEvidence(usage=usage, metadata=metadata)


def _invalid_evidence() -> Never:
    raise ValueError("Bedrock smoke provider evidence is invalid")
