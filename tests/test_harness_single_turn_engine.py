import asyncio
import hashlib
from collections.abc import AsyncIterator

import pytest

from app.services.harness.protocol import (
    ExecutionBudget,
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderFailureClass,
    ProviderFinishReason,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderTokenUsage,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.sessions import (
    SingleTurnEngine,
    SingleTurnOutcome,
    ToolResolution,
    ToolResolutionStatus,
    TurnBudgetSnapshot,
    TurnEnginePhase,
    TurnLoopLimits,
    TurnProviderTrace,
    TurnTerminalTrace,
    TurnToolExchangeTrace,
    initial_turn_budget,
)

TURN_ID = "trn_" + "3" * 32


def budget(
    *,
    stream_bytes: int = 10_000,
    tool_output_bytes: int = 10_000,
) -> TurnBudgetSnapshot:
    return initial_turn_budget(
        TURN_ID,
        ExecutionBudget(
            max_steps=8,
            max_tool_calls=4,
            max_input_tokens=100,
            max_output_tokens=100,
            max_tool_output_bytes=tool_output_bytes,
            max_duration_ms=10_000,
            max_cost_microusd=10_000,
        ),
        TurnLoopLimits(
            max_provider_retries=2,
            max_identical_calls=2,
            max_stream_bytes=stream_bytes,
        ),
    )


def usage(sequence: int = 2) -> ProviderUsage:
    return ProviderUsage(
        sequence=sequence,
        usage=ProviderTokenUsage(
            input_tokens=10,
            cached_input_tokens=2,
            output_tokens=3,
            reasoning_tokens=1,
            cost_microusd=12,
        ),
    )


def tool_call() -> ProviderToolCall:
    arguments_json = '{"path":"README.md"}'
    return ProviderToolCall(
        sequence=1,
        call_id="call_00000001",
        tool_name="workspace.read_file",
        arguments_json=arguments_json,
        arguments_sha256=hashlib.sha256(arguments_json.encode()).hexdigest(),
    )


class Attempts:
    def __init__(
        self,
        attempts: tuple[
            tuple[ProviderStreamEvent, ...] | Exception,
            ...,
        ],
    ) -> None:
        self._attempts = attempts

    def open_attempt(
        self,
        attempt: int,
    ) -> AsyncIterator[ProviderStreamEvent]:
        async def stream() -> AsyncIterator[ProviderStreamEvent]:
            if attempt > len(self._attempts):
                return
            configured = self._attempts[attempt - 1]
            if isinstance(configured, Exception):
                raise configured
            for event in configured:
                yield event

        return stream()


class Resolver:
    def __init__(
        self,
        status: ToolResolutionStatus = ToolResolutionStatus.ALLOWED,
    ) -> None:
        self.status = status
        self.calls: list[ProviderToolCall] = []

    async def resolve(self, call: ProviderToolCall) -> ToolResolution:
        self.calls.append(call)
        value = b'{"status":"ok"}'
        return ToolResolution(
            status=self.status,
            result_sha256=hashlib.sha256(value).hexdigest(),
            result_bytes=len(value),
            reason=(
                "Tool use was denied."
                if self.status is ToolResolutionStatus.DENIED
                else None
            ),
        )


def run(
    attempts: Attempts,
    *,
    resolver: Resolver | None = None,
    stream_bytes: int = 10_000,
) -> SingleTurnOutcome:
    engine = SingleTurnEngine(
        attempts,
        resolver or Resolver(),
        monotonic_clock=lambda: 0.0,
    )
    return asyncio.run(engine.run(budget(stream_bytes=stream_bytes)))


def terminal(outcome: SingleTurnOutcome) -> TurnTerminalTrace:
    event = outcome.events[-1]
    assert isinstance(event, TurnTerminalTrace)
    return event


def test_happy_stream_requires_final_usage_and_completes() -> None:
    outcome = run(
        Attempts(
            (
                (
                    ProviderTextDelta(sequence=1, text="Hello"),
                    usage(),
                    ProviderCompleted(
                        sequence=3,
                        finish_reason=ProviderFinishReason.STOP,
                    ),
                ),
            )
        )
    )

    assert outcome.phase is TurnEnginePhase.COMPLETED
    assert outcome.budget.usage.steps == 1
    assert outcome.budget.usage.input_tokens == 10
    assert outcome.budget.usage.output_tokens == 4
    assert outcome.budget.usage.cost_microusd == 12
    assert terminal(outcome).budget == outcome.budget


def test_retry_is_bounded_and_usage_belongs_to_successful_attempt() -> None:
    outcome = run(
        Attempts(
            (
                (
                    ProviderError(
                        sequence=1,
                        failure_class=ProviderFailureClass.RATE_LIMIT,
                        retry_allowed=True,
                        reason="Retry later.",
                    ),
                ),
                (
                    usage(sequence=1),
                    ProviderCompleted(
                        sequence=2,
                        finish_reason=ProviderFinishReason.STOP,
                    ),
                ),
            )
        )
    )

    assert outcome.phase is TurnEnginePhase.COMPLETED
    assert outcome.budget.usage.steps == 2
    assert outcome.budget.provider_retries == 1


def test_tool_call_and_denial_are_one_atomic_durable_exchange() -> None:
    resolver = Resolver(ToolResolutionStatus.DENIED)
    outcome = run(
        Attempts(
            (
                (
                    tool_call(),
                    usage(),
                    ProviderCompleted(
                        sequence=3,
                        finish_reason=ProviderFinishReason.TOOL_CALLS,
                    ),
                ),
                (
                    usage(sequence=1),
                    ProviderCompleted(
                        sequence=2,
                        finish_reason=ProviderFinishReason.STOP,
                    ),
                ),
            )
        ),
        resolver=resolver,
    )
    exchanges = tuple(
        event
        for event in outcome.events
        if isinstance(event, TurnToolExchangeTrace)
    )
    provider_tool_calls = tuple(
        event
        for event in outcome.events
        if isinstance(event, TurnProviderTrace)
        and isinstance(event.event, ProviderToolCall)
    )

    assert outcome.phase is TurnEnginePhase.COMPLETED
    assert len(exchanges) == 1
    assert exchanges[0].resolution.status is ToolResolutionStatus.DENIED
    assert exchanges[0].resolution.durable
    assert provider_tool_calls == ()
    assert outcome.budget.usage.tool_calls == 1
    assert len(resolver.calls) == 1


@pytest.mark.parametrize(
    ("attempts", "phase", "reason"),
    (
        (Attempts(((),)), TurnEnginePhase.FAILED, "provider_stream_empty"),
        (
            Attempts((RuntimeError("malformed"),)),
            TurnEnginePhase.FAILED,
            "provider_stream_failed",
        ),
        (
            Attempts(
                (
                    (
                        ProviderCancelled(
                            sequence=1,
                            reason="Caller cancelled.",
                        ),
                    ),
                )
            ),
            TurnEnginePhase.CANCELLED,
            "provider_cancelled",
        ),
        (
            Attempts(
                (
                    (
                        ProviderCompleted(
                            sequence=1,
                            finish_reason=ProviderFinishReason.STOP,
                        ),
                    ),
                )
            ),
            TurnEnginePhase.FAILED,
            "provider_usage_missing",
        ),
    ),
)
def test_terminal_error_empty_malformed_and_cancelled_paths(
    attempts: Attempts,
    phase: TurnEnginePhase,
    reason: str,
) -> None:
    outcome = run(attempts)

    assert outcome.phase is phase
    assert terminal(outcome).reason == reason


def test_stream_budget_exhaustion_is_terminal_and_atomic() -> None:
    initial = budget(stream_bytes=1)
    engine = SingleTurnEngine(
        Attempts(((ProviderTextDelta(sequence=1, text="too large"),),)),
        Resolver(),
        monotonic_clock=lambda: 0.0,
    )
    outcome = asyncio.run(engine.run(initial))

    assert outcome.phase is TurnEnginePhase.FAILED
    assert terminal(outcome).reason == "budget_exhausted:stream_bytes"
    assert outcome.budget.stream_bytes == 0


def test_durable_tool_result_remains_atomically_traced_on_budget_failure() -> None:
    initial = budget(tool_output_bytes=1)
    engine = SingleTurnEngine(
        Attempts(((tool_call(),),)),
        Resolver(),
        monotonic_clock=lambda: 0.0,
    )
    outcome = asyncio.run(engine.run(initial))
    exchanges = tuple(
        event
        for event in outcome.events
        if isinstance(event, TurnToolExchangeTrace)
    )

    assert outcome.phase is TurnEnginePhase.FAILED
    assert terminal(outcome).reason == "budget_exhausted:tool_output_bytes"
    assert len(exchanges) == 1
    assert exchanges[0].resolution.durable
    assert outcome.budget.usage.tool_output_bytes == 0
