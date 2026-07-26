"""Bounded verification of provider smoke response streams."""

from __future__ import annotations

from dataclasses import dataclass

from app.cli.harness.provider_smoke_io import sse_data_records
from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderError,
    ProviderReasoningDelta,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderUsage,
)
from app.services.harness.providers import (
    OpenAIResponsesDecoder,
    OpenRouterResponsesDecoder,
)

EXPECTED_SMOKE_RESPONSE = "ATLAS_SMOKE_OK"


@dataclass(frozen=True, slots=True)
class ProviderSmokeStreamEvidence:
    usage: ProviderTokenUsage
    routing_metadata_sha256: str | None


def decode_provider_smoke_response(
    provider: str,
    body: bytes,
    *,
    charged_cost_microusd: int,
) -> ProviderSmokeStreamEvidence:
    def fixed_cost(
        _input_tokens: int,
        _cached_input_tokens: int,
        _output_tokens: int,
        _reasoning_tokens: int,
    ) -> int:
        return charged_cost_microusd

    decoder = (
        OpenRouterResponsesDecoder(fixed_cost)
        if provider == "openrouter"
        else OpenAIResponsesDecoder(fixed_cost)
    )
    expected_offset = 0
    usage: ProviderTokenUsage | None = None
    completed = False
    for record in sse_data_records(body):
        if record == b"[DONE]":
            if provider == "openrouter":
                decoder.decode(record)
            elif not completed:
                raise ValueError("OpenAI smoke ended before completion")
            continue
        for event in decoder.decode(record):
            if isinstance(event, ProviderTextDelta):
                expected_offset = _verify_text_delta(
                    event.text,
                    expected_offset,
                )
            elif isinstance(event, ProviderUsage):
                if usage is not None:
                    raise ValueError("provider smoke emitted duplicate usage")
                usage = event.usage
            elif isinstance(event, ProviderCompleted):
                if completed:
                    raise ValueError("provider smoke emitted duplicate completion")
                completed = True
            elif isinstance(event, ProviderError):
                raise ValueError("provider smoke returned a provider error")
            elif not isinstance(event, ProviderReasoningDelta):
                raise ValueError("provider smoke returned an unexpected event")
    if (
        expected_offset != len(EXPECTED_SMOKE_RESPONSE)
        or usage is None
        or not completed
    ):
        raise ValueError("provider smoke response evidence is incomplete")
    routing_metadata = getattr(decoder, "routing_metadata", None)
    return ProviderSmokeStreamEvidence(
        usage=usage,
        routing_metadata_sha256=(
            routing_metadata.content_sha256 if routing_metadata is not None else None
        ),
    )


def _verify_text_delta(text: str, expected_offset: int) -> int:
    next_offset = expected_offset + len(text)
    expected = EXPECTED_SMOKE_RESPONSE[expected_offset:next_offset]
    if text != expected or next_offset > len(EXPECTED_SMOKE_RESPONSE):
        raise ValueError("provider smoke returned unexpected text")
    return next_offset
