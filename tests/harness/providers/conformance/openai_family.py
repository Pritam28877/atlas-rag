"""Checksum-pinned OpenAI-family conformance adapter composition."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

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
from app.services.harness.providers.conformance_resources import (
    CONFORMANCE_FIXTURE_ROOT,
)
from app.services.harness.providers.openai_decoder import (
    OpenAIResponsesDecoder,
)
from app.services.harness.providers.openrouter_decoder import (
    OpenRouterResponsesDecoder,
)
from tests.harness.providers.conformance.fixtures import NOW

FIXTURES = CONFORMANCE_FIXTURE_ROOT / "openai_family"
SOURCE_ROOT = Path(__file__).parents[4] / "app" / "services" / "harness"


def openai_family_adapters() -> tuple[
    RecordedDecoderConformanceAdapter,
    ...,
]:
    cases = _cases()
    return (
        _adapter(
            "local-compatible",
            cases,
            lambda: OpenAIResponsesDecoder(_cost),
            ("providers/openai_decoder.py",),
        ),
        _adapter(
            "openai",
            cases,
            lambda: OpenAIResponsesDecoder(_cost),
            ("providers/openai_decoder.py",),
        ),
        _adapter(
            "openrouter",
            cases,
            lambda: OpenRouterResponsesDecoder(_cost),
            (
                "providers/openai_decoder.py",
                "providers/openrouter_decoder.py",
            ),
        ),
    )


def _cases() -> tuple[RecordedConformanceCase, ...]:
    return (
        _case(
            ConformanceScenario.CANCELLATION,
            RecordedConformanceCaseMode.CANCEL,
        ),
        _case(
            ConformanceScenario.MALFORMED_STREAM,
            RecordedConformanceCaseMode.EXPECT_DECODE_ERROR,
            "malformed.jsonl",
        ),
        _case(
            ConformanceScenario.OUTPUT_LIMIT,
            RecordedConformanceCaseMode.DECODE,
            "output_limit.jsonl",
        ),
        _case(
            ConformanceScenario.POLICY,
            RecordedConformanceCaseMode.DECODE,
            "policy.jsonl",
        ),
        _case(
            ConformanceScenario.REASONING_STREAM,
            RecordedConformanceCaseMode.DECODE,
            "reasoning.jsonl",
        ),
        _case(
            ConformanceScenario.TEXT_STREAM,
            RecordedConformanceCaseMode.DECODE,
            "text.jsonl",
        ),
        _case(
            ConformanceScenario.TOOL_CALLS,
            RecordedConformanceCaseMode.DECODE,
            "tools.jsonl",
        ),
        _case(
            ConformanceScenario.USAGE_COST,
            RecordedConformanceCaseMode.DECODE,
            "text.jsonl",
        ),
    )


def _case(
    scenario: ConformanceScenario,
    mode: RecordedConformanceCaseMode,
    fixture_name: str | None = None,
) -> RecordedConformanceCase:
    if fixture_name is None:
        records: tuple[bytes, ...] = ()
        expected_digest = recorded_records_sha256(records)
    else:
        records = tuple((FIXTURES / fixture_name).read_bytes().splitlines())
        manifest = json.loads((FIXTURES / "manifest.json").read_text())
        expected_digest = manifest[fixture_name]
    return RecordedConformanceCase(
        scenario=scenario,
        mode=mode,
        records=records,
        records_sha256=expected_digest,
    )


def _adapter(
    provider: str,
    cases: tuple[RecordedConformanceCase, ...],
    decoder_factory: Callable[[], ConformanceDecoder],
    source_paths: tuple[str, ...],
) -> RecordedDecoderConformanceAdapter:
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
