"""Checksum-pinned recorded decoder adapter for conformance runs."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderError,
    ProviderFailureClass,
    ProviderName,
    ProviderStreamEvent,
    Sha256,
    StrictProtocolModel,
)
from app.services.harness.providers.conformance_contracts import (
    MAXIMUM_CONFORMANCE_SCENARIOS,
    ConformanceAdapterDescriptor,
    ConformanceScenario,
)
from app.services.harness.providers.provider_stream_control import (
    remaining_seconds,
)
from app.services.harness.providers.recorded import (
    MAXIMUM_RECORDED_EVENT_BYTES,
    MAXIMUM_RECORDED_EVENTS,
    MAXIMUM_RECORDED_STREAM_BYTES,
)

MAXIMUM_RECORDED_ADAPTER_BYTES = 32 * 1024 * 1024


class RecordedConformanceCaseMode(StrEnum):
    CANCEL = "cancel"
    DECODE = "decode"
    EXPECT_DECODE_ERROR = "expect_decode_error"


class RecordedConformanceCase(StrictProtocolModel):
    scenario: ConformanceScenario
    mode: RecordedConformanceCaseMode
    records: tuple[bytes, ...] = Field(
        max_length=MAXIMUM_RECORDED_EVENTS,
    )
    records_sha256: Sha256

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        has_records = bool(self.records)
        if has_records != (
            self.mode is not RecordedConformanceCaseMode.CANCEL
        ):
            raise ValueError(
                "recorded conformance case mode is inconsistent"
            )
        total_bytes = 0
        for record in self.records:
            if not 1 <= len(record) <= MAXIMUM_RECORDED_EVENT_BYTES:
                raise ValueError(
                    "recorded conformance event size is invalid"
                )
            total_bytes += len(record)
        if (
            total_bytes > MAXIMUM_RECORDED_STREAM_BYTES
            or recorded_records_sha256(self.records)
            != self.records_sha256
        ):
            raise ValueError(
                "recorded conformance fixture is invalid"
            )
        return self


class ConformanceDecoder(Protocol):
    def decode(
        self,
        record: bytes,
    ) -> tuple[ProviderStreamEvent, ...]: ...

    def cancel(self) -> ProviderCancelled: ...


class RecordedConformanceAdapterErrorCode(StrEnum):
    EXPECTED_FAILURE = "expected_failure"
    SCENARIO = "scenario"


class RecordedConformanceAdapterError(RuntimeError):
    def __init__(
        self,
        code: RecordedConformanceAdapterErrorCode,
    ) -> None:
        super().__init__("recorded conformance adapter failed")
        self.code = code


class RecordedDecoderConformanceAdapter:
    def __init__(
        self,
        *,
        provider: ProviderName,
        adapter_revision_sha256: Sha256,
        cases: tuple[RecordedConformanceCase, ...],
        decoder_factory: Callable[[], ConformanceDecoder],
        clock: Callable[[], datetime],
    ) -> None:
        if (
            not 1 <= len(cases) <= MAXIMUM_CONFORMANCE_SCENARIOS
            or tuple(
                sorted(set(case.scenario for case in cases))
            )
            != tuple(case.scenario for case in cases)
            or sum(
                len(record)
                for case in cases
                for record in case.records
            )
            > MAXIMUM_RECORDED_ADAPTER_BYTES
        ):
            raise ValueError(
                "recorded conformance cases are not canonical"
            )
        self._descriptor = ConformanceAdapterDescriptor(
            provider=provider,
            adapter_revision_sha256=adapter_revision_sha256,
            supported_scenarios=tuple(
                case.scenario for case in cases
            ),
        )
        self._cases = {case.scenario: case for case in cases}
        self._decoder_factory = decoder_factory
        self._clock = clock

    @property
    def descriptor(self) -> ConformanceAdapterDescriptor:
        return self._descriptor

    async def stream(
        self,
        scenario: ConformanceScenario,
        *,
        cancellation: asyncio.Event,
        deadline_at: datetime,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        case = self._cases.get(scenario)
        if case is None:
            raise RecordedConformanceAdapterError(
                RecordedConformanceAdapterErrorCode.SCENARIO
            )
        decoder = self._decoder_factory()
        if case.mode is RecordedConformanceCaseMode.CANCEL:
            _require_active(self._clock, cancellation, deadline_at)
            yield decoder.cancel()
            return
        emitted_events = 0
        for record in case.records:
            _require_active(self._clock, cancellation, deadline_at)
            await asyncio.sleep(0)
            try:
                events = decoder.decode(record)
            except Exception:
                if (
                    case.mode
                    is not RecordedConformanceCaseMode.EXPECT_DECODE_ERROR
                ):
                    raise
                yield ProviderError(
                    sequence=emitted_events + 1,
                    failure_class=ProviderFailureClass.MALFORMED,
                    retry_allowed=False,
                    reason=(
                        "Recorded decoder rejected malformed input."
                    ),
                )
                return
            for event in events:
                emitted_events += 1
                yield event
        if case.mode is RecordedConformanceCaseMode.EXPECT_DECODE_ERROR:
            raise RecordedConformanceAdapterError(
                RecordedConformanceAdapterErrorCode.EXPECTED_FAILURE
            )


def recorded_records_sha256(records: tuple[bytes, ...]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return digest.hexdigest()


def _require_active(
    clock: Callable[[], datetime],
    cancellation: asyncio.Event,
    deadline_at: datetime,
) -> None:
    if cancellation.is_set():
        raise asyncio.CancelledError
    remaining_seconds(clock, deadline_at)
