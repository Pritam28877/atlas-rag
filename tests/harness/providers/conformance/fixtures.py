"""Bounded synthetic adapters for provider-neutral conformance tests."""

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime

from app.services.harness.protocol import (
    ProviderCompleted,
    ProviderFinishReason,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.providers import (
    ConformanceAdapterDescriptor,
    ConformanceScenario,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
type CaseValue = (
    tuple[ProviderStreamEvent, ...]
    | Exception
    | Callable[[], AsyncGenerator[ProviderStreamEvent, None]]
)


class StaticConformanceAdapter:
    def __init__(
        self,
        provider: str,
        cases: dict[ConformanceScenario, CaseValue],
    ) -> None:
        revision = hashlib.sha256(
            f"{provider}-adapter-v1".encode()
        ).hexdigest()
        self._descriptor = ConformanceAdapterDescriptor(
            provider=provider,
            adapter_revision_sha256=revision,
            supported_scenarios=tuple(sorted(cases)),
        )
        self._cases = cases
        self.calls: list[ConformanceScenario] = []
        self.closed_streams = 0

    @property
    def descriptor(self) -> ConformanceAdapterDescriptor:
        return self._descriptor

    def stream(
        self,
        scenario: ConformanceScenario,
        **kwargs,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        del kwargs
        self.calls.append(scenario)
        value = self._cases[scenario]
        if callable(value):
            return value()
        return self._stream(value)

    async def _stream(
        self,
        value: tuple[ProviderStreamEvent, ...] | Exception,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        try:
            if isinstance(value, Exception):
                raise value
            for event in value:
                yield event
        finally:
            self.closed_streams += 1


def text_events(
    *chunks: str,
    input_tokens: int = 2,
    output_tokens: int = 1,
) -> tuple[ProviderStreamEvent, ...]:
    events: list[ProviderStreamEvent] = [
        ProviderTextDelta(sequence=index, text=chunk)
        for index, chunk in enumerate(chunks, start=1)
    ]
    events.extend(
        (
            ProviderUsage(
                sequence=len(events) + 1,
                usage=ProviderTokenUsage(
                    input_tokens=input_tokens,
                    cached_input_tokens=0,
                    output_tokens=output_tokens,
                    reasoning_tokens=0,
                    cost_microusd=input_tokens + output_tokens,
                ),
            ),
            ProviderCompleted(
                sequence=len(events) + 2,
                finish_reason=ProviderFinishReason.STOP,
            ),
        )
    )
    return tuple(events)


def tool_events(
    call_id: str,
) -> tuple[ProviderStreamEvent, ...]:
    arguments = json.dumps(
        {"city": "Pune"},
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        ProviderToolCall(
            sequence=1,
            call_id=call_id,
            tool_name="weather.lookup",
            arguments_json=arguments,
            arguments_sha256=hashlib.sha256(
                arguments.encode()
            ).hexdigest(),
        ),
        ProviderUsage(
            sequence=2,
            usage=ProviderTokenUsage(
                input_tokens=4,
                cached_input_tokens=0,
                output_tokens=2,
                reasoning_tokens=0,
                cost_microusd=6,
            ),
        ),
        ProviderCompleted(
            sequence=3,
            finish_reason=ProviderFinishReason.TOOL_CALLS,
        ),
    )


def blocked_stream() -> AsyncGenerator[ProviderStreamEvent, None]:
    async def stream() -> AsyncGenerator[ProviderStreamEvent, None]:
        await asyncio.Event().wait()
        if False:
            yield ProviderCompleted(
                sequence=1,
                finish_reason=ProviderFinishReason.STOP,
            )

    return stream()
