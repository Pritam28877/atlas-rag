"""Production-owned checksum-pinned provider conformance suite."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderName,
    ProviderStreamEvent,
)
from app.services.harness.providers.bedrock_decoder import (
    BedrockConverseStreamDecoder,
)
from app.services.harness.providers.conformance_recorded import (
    ConformanceDecoder,
    RecordedConformanceCase,
    RecordedDecoderConformanceAdapter,
)
from app.services.harness.providers.conformance_recording_loader import (
    load_recorded_conformance_cases,
)
from app.services.harness.providers.openai_decoder import (
    OpenAIResponsesDecoder,
)
from app.services.harness.providers.openrouter_decoder import (
    OpenRouterResponsesDecoder,
)
from app.services.harness.providers.vertex_decoder import (
    VertexGenerateContentDecoder,
)

Clock = Callable[[], datetime]
DecoderFactory = Callable[[], ConformanceDecoder]
HARNESS_SOURCE_ROOT = Path(__file__).parents[1]


class BedrockBytesDecoder:
    def __init__(self) -> None:
        self._delegate = BedrockConverseStreamDecoder(_cost)

    def decode(
        self,
        record: bytes,
    ) -> tuple[ProviderStreamEvent, ...]:
        event = json.loads(record)
        if not isinstance(event, dict):
            raise ValueError("Bedrock fixture event is not an object")
        return self._delegate.decode(cast(dict[str, object], event))

    def cancel(self) -> ProviderCancelled:
        return self._delegate.cancel()


class CanonicalEventDecoder:
    def __init__(self) -> None:
        self._adapter: TypeAdapter[ProviderStreamEvent] = TypeAdapter(
            ProviderStreamEvent
        )

    def decode(
        self,
        record: bytes,
    ) -> tuple[ProviderStreamEvent, ...]:
        return (self._adapter.validate_json(record),)

    def cancel(self) -> ProviderCancelled:
        return ProviderCancelled(
            sequence=1,
            reason="Mock conformance cancellation.",
        )


def recorded_conformance_adapters(
    *,
    clock: Clock,
) -> tuple[RecordedDecoderConformanceAdapter, ...]:
    return (
        *native_cloud_conformance_adapters(clock=clock),
        mock_conformance_adapter(clock=clock),
        *openai_family_conformance_adapters(clock=clock),
    )


def native_cloud_conformance_adapters(
    *,
    clock: Clock,
) -> tuple[RecordedDecoderConformanceAdapter, ...]:
    return (
        _adapter(
            provider="bedrock",
            cases=load_recorded_conformance_cases("bedrock"),
            decoder_factory=BedrockBytesDecoder,
            source_paths=(
                "providers/bedrock_decoder.py",
                "providers/bedrock_decode_support.py",
            ),
            clock=clock,
        ),
        _adapter(
            provider="vertex",
            cases=load_recorded_conformance_cases("vertex"),
            decoder_factory=_vertex_decoder,
            source_paths=(
                "providers/vertex_content_decoder.py",
                "providers/vertex_decoder.py",
            ),
            clock=clock,
        ),
    )


def openai_family_conformance_adapters(
    *,
    clock: Clock,
) -> tuple[RecordedDecoderConformanceAdapter, ...]:
    cases = load_recorded_conformance_cases("openai_family")
    return (
        _adapter(
            provider="local-compatible",
            cases=cases,
            decoder_factory=_openai_decoder,
            source_paths=("providers/openai_decoder.py",),
            clock=clock,
        ),
        _adapter(
            provider="openai",
            cases=cases,
            decoder_factory=_openai_decoder,
            source_paths=("providers/openai_decoder.py",),
            clock=clock,
        ),
        _adapter(
            provider="openrouter",
            cases=cases,
            decoder_factory=_openrouter_decoder,
            source_paths=(
                "providers/openai_decoder.py",
                "providers/openrouter_decoder.py",
            ),
            clock=clock,
        ),
    )


def mock_conformance_adapter(
    *,
    clock: Clock,
) -> RecordedDecoderConformanceAdapter:
    return _adapter(
        provider="mock",
        cases=load_recorded_conformance_cases("mock"),
        decoder_factory=CanonicalEventDecoder,
        source_paths=(
            "providers/recorded.py",
            "protocol/provider_stream.py",
        ),
        clock=clock,
    )


def _adapter(
    *,
    provider: ProviderName,
    cases: tuple[RecordedConformanceCase, ...],
    decoder_factory: DecoderFactory,
    source_paths: tuple[str, ...],
    clock: Clock,
) -> RecordedDecoderConformanceAdapter:
    digest = hashlib.sha256(provider.encode())
    for source_path in source_paths:
        digest.update((HARNESS_SOURCE_ROOT / source_path).read_bytes())
    for case in cases:
        digest.update(case.records_sha256.encode())
    return RecordedDecoderConformanceAdapter(
        provider=provider,
        adapter_revision_sha256=digest.hexdigest(),
        cases=cases,
        decoder_factory=decoder_factory,
        clock=clock,
    )


def _openai_decoder() -> OpenAIResponsesDecoder:
    return OpenAIResponsesDecoder(_cost)


def _openrouter_decoder() -> OpenRouterResponsesDecoder:
    return OpenRouterResponsesDecoder(_cost)


def _vertex_decoder() -> VertexGenerateContentDecoder:
    return VertexGenerateContentDecoder(_cost)


def _cost(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> int:
    return (
        input_tokens
        + cached_tokens
        + output_tokens
        + reasoning_tokens
    )
