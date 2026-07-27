"""Checksum-pinned canonical mock-provider conformance composition."""

import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderStreamEvent,
)
from app.services.harness.providers.conformance_contracts import (
    ConformanceScenario,
)
from app.services.harness.providers.conformance_recorded import (
    RecordedConformanceCase,
    RecordedConformanceCaseMode,
    RecordedDecoderConformanceAdapter,
    recorded_records_sha256,
)
from tests.harness.providers.conformance.fixtures import NOW

FIXTURES = (
    Path(__file__).parents[3]
    / "fixtures"
    / "harness"
    / "conformance"
    / "mock"
)
SOURCE_ROOT = Path(__file__).parents[4] / "app" / "services" / "harness"


class CanonicalEventDecoder:
    def __init__(self) -> None:
        self._adapter = TypeAdapter(ProviderStreamEvent)

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


def mock_conformance_adapter() -> RecordedDecoderConformanceAdapter:
    cases = _cases()
    digest = hashlib.sha256(b"mock")
    digest.update(
        (SOURCE_ROOT / "providers/recorded.py").read_bytes()
    )
    digest.update(
        (SOURCE_ROOT / "protocol/provider_stream.py").read_bytes()
    )
    for case in cases:
        digest.update(case.records_sha256.encode())
    return RecordedDecoderConformanceAdapter(
        provider="mock",
        adapter_revision_sha256=digest.hexdigest(),
        cases=cases,
        decoder_factory=CanonicalEventDecoder,
        clock=lambda: NOW,
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
