import asyncio
import hashlib
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderFinishReason,
    ProviderTextDelta,
)
from app.services.harness.providers import ConformanceScenario
from app.services.harness.providers.conformance_recorded import (
    RecordedConformanceCase,
    RecordedConformanceCaseMode,
    RecordedDecoderConformanceAdapter,
    recorded_records_sha256,
)
from app.services.harness.providers.conformance_runner import (
    BoundedConformanceRunner,
)
from tests.harness.providers.conformance.fixtures import NOW


class FixtureDecoder:
    def decode(self, record: bytes):
        if record == b"malformed":
            raise ValueError("secret provider detail")
        return (
            ProviderTextDelta(sequence=1, text=record.decode()),
            ProviderCompleted(
                sequence=2,
                finish_reason=ProviderFinishReason.STOP,
            ),
        )

    def cancel(self) -> ProviderCancelled:
        return ProviderCancelled(
            sequence=1,
            reason="Recorded cancellation.",
        )


def test_replays_decode_cancel_and_expected_failure_cases() -> None:
    cases = (
        _case(
            ConformanceScenario.CANCELLATION,
            RecordedConformanceCaseMode.CANCEL,
            (),
        ),
        _case(
            ConformanceScenario.MALFORMED_STREAM,
            RecordedConformanceCaseMode.EXPECT_DECODE_ERROR,
            (b"malformed",),
        ),
        _case(
            ConformanceScenario.TEXT_STREAM,
            RecordedConformanceCaseMode.DECODE,
            (b"answer",),
        ),
    )
    adapter = RecordedDecoderConformanceAdapter(
        provider="openai",
        adapter_revision_sha256=hashlib.sha256(
            b"openai-recorded-v1"
        ).hexdigest(),
        cases=cases,
        decoder_factory=FixtureDecoder,
        clock=lambda: NOW,
    )

    text = _events(adapter, ConformanceScenario.TEXT_STREAM)
    malformed = _events(
        adapter,
        ConformanceScenario.MALFORMED_STREAM,
    )
    cancelled = _events(adapter, ConformanceScenario.CANCELLATION)

    assert isinstance(text[-1], ProviderCompleted)
    assert malformed[0].failure_class.value == "malformed"
    assert "secret provider detail" not in malformed[0].reason
    assert isinstance(cancelled[0], ProviderCancelled)

    report = asyncio.run(
        BoundedConformanceRunner(clock=lambda: NOW).run(
            (adapter,),
            scenarios=tuple(case.scenario for case in cases),
            cancellation=asyncio.Event(),
            deadline_at=NOW + timedelta(seconds=5),
        )
    )
    assert all(
        observation.status.value == "passed"
        for observation in report.observations
    )


def test_fixture_checksum_and_case_order_fail_closed() -> None:
    with pytest.raises(ValidationError):
        RecordedConformanceCase(
            scenario=ConformanceScenario.TEXT_STREAM,
            mode=RecordedConformanceCaseMode.DECODE,
            records=(b"answer",),
            records_sha256="0" * 64,
        )

    text = _case(
        ConformanceScenario.TEXT_STREAM,
        RecordedConformanceCaseMode.DECODE,
        (b"answer",),
    )
    cancellation = _case(
        ConformanceScenario.CANCELLATION,
        RecordedConformanceCaseMode.CANCEL,
        (),
    )
    with pytest.raises(ValueError, match="not canonical"):
        RecordedDecoderConformanceAdapter(
            provider="openai",
            adapter_revision_sha256="1" * 64,
            cases=(text, cancellation),
            decoder_factory=FixtureDecoder,
            clock=lambda: NOW,
        )


def _case(
    scenario: ConformanceScenario,
    mode: RecordedConformanceCaseMode,
    records: tuple[bytes, ...],
) -> RecordedConformanceCase:
    return RecordedConformanceCase(
        scenario=scenario,
        mode=mode,
        records=records,
        records_sha256=recorded_records_sha256(records),
    )


def _events(
    adapter: RecordedDecoderConformanceAdapter,
    scenario: ConformanceScenario,
):
    async def collect():
        return [
            event
            async for event in adapter.stream(
                scenario,
                cancellation=asyncio.Event(),
                deadline_at=NOW + timedelta(seconds=1),
            )
        ]

    return asyncio.run(collect())
