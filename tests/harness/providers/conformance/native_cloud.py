"""Checksum-pinned Bedrock and Vertex conformance composition."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderStreamEvent,
)
from app.services.harness.providers.bedrock_decoder import (
    BedrockConverseStreamDecoder,
)
from app.services.harness.providers.conformance_contracts import (
    ConformanceScenario,
)
from app.services.harness.providers.conformance_recorded import (
    ConformanceDecoder,
    RecordedConformanceCase,
    RecordedConformanceCaseMode,
    RecordedDecoderConformanceAdapter,
    recorded_records_sha256,
)
from app.services.harness.providers.vertex_decoder import (
    VertexGenerateContentDecoder,
)
from tests.harness.providers.conformance.fixtures import NOW

FIXTURES = (
    Path(__file__).parents[3]
    / "fixtures"
    / "harness"
    / "conformance"
)
SOURCE_ROOT = Path(__file__).parents[4] / "app" / "services" / "harness"


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
        return self._delegate.decode(
            cast(dict[str, object], event)
        )

    def cancel(self) -> ProviderCancelled:
        return self._delegate.cancel()


def native_cloud_adapters() -> tuple[
    RecordedDecoderConformanceAdapter,
    ...,
]:
    return (
        _adapter(
            "bedrock",
            BedrockBytesDecoder,
            (
                "providers/bedrock_decoder.py",
                "providers/bedrock_decode_support.py",
            ),
        ),
        _adapter(
            "vertex",
            lambda: VertexGenerateContentDecoder(_cost),
            (
                "providers/vertex_content_decoder.py",
                "providers/vertex_decoder.py",
            ),
        ),
    )


def _adapter(
    provider: str,
    decoder_factory: Callable[[], ConformanceDecoder],
    source_paths: tuple[str, ...],
) -> RecordedDecoderConformanceAdapter:
    cases = _cases(provider)
    digest = hashlib.sha256(provider.encode())
    for path in source_paths:
        digest.update((SOURCE_ROOT / path).read_bytes())
    for case in cases:
        digest.update(case.records_sha256.encode())
    return RecordedDecoderConformanceAdapter(
        provider=provider,
        adapter_revision_sha256=digest.hexdigest(),
        cases=cases,
        decoder_factory=decoder_factory,
        clock=lambda: NOW,
    )


def _cases(provider: str) -> tuple[RecordedConformanceCase, ...]:
    return (
        _case(
            provider,
            ConformanceScenario.CANCELLATION,
            RecordedConformanceCaseMode.CANCEL,
        ),
        _case(
            provider,
            ConformanceScenario.MALFORMED_STREAM,
            RecordedConformanceCaseMode.EXPECT_DECODE_ERROR,
            "malformed.jsonl",
        ),
        _case(
            provider,
            ConformanceScenario.OUTPUT_LIMIT,
            RecordedConformanceCaseMode.DECODE,
            "output_limit.jsonl",
        ),
        _case(
            provider,
            ConformanceScenario.POLICY,
            RecordedConformanceCaseMode.DECODE,
            "policy.jsonl",
        ),
        _case(
            provider,
            ConformanceScenario.REASONING_STREAM,
            RecordedConformanceCaseMode.DECODE,
            "reasoning.jsonl",
        ),
        _case(
            provider,
            ConformanceScenario.TEXT_STREAM,
            RecordedConformanceCaseMode.DECODE,
            "text.jsonl",
        ),
        _case(
            provider,
            ConformanceScenario.TOOL_CALLS,
            RecordedConformanceCaseMode.DECODE,
            "tools.jsonl",
        ),
        _case(
            provider,
            ConformanceScenario.USAGE_COST,
            RecordedConformanceCaseMode.DECODE,
            "text.jsonl",
        ),
    )


def _case(
    provider: str,
    scenario: ConformanceScenario,
    mode: RecordedConformanceCaseMode,
    fixture_name: str | None = None,
) -> RecordedConformanceCase:
    if fixture_name is None:
        records: tuple[bytes, ...] = ()
        expected_digest = recorded_records_sha256(records)
    else:
        root = FIXTURES / provider
        records = tuple((root / fixture_name).read_bytes().splitlines())
        manifest = json.loads((root / "manifest.json").read_text())
        expected_digest = manifest[fixture_name]
    return RecordedConformanceCase(
        scenario=scenario,
        mode=mode,
        records=records,
        records_sha256=expected_digest,
    )


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
