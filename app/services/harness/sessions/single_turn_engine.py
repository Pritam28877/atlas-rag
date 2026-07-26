"""Deterministic bounded single-turn execution over canonical provider events."""

from __future__ import annotations

import time
from collections.abc import Callable

from app.services.harness.protocol import (
    ProviderCancelled,
    ProviderCompleted,
    ProviderError,
    ProviderFinishReason,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.sessions.single_turn_contracts import (
    ProviderAttemptSource,
    SingleTurnOutcome,
    TurnToolExchangeTrace,
    TurnToolResolver,
    TurnTraceEvent,
)
from app.services.harness.sessions.single_turn_support import (
    TurnTraceLimit,
    append_lifecycle,
    append_provider,
    append_trace,
    charge_provider_usage,
    failed_tool_resolution,
    require_trace_capacity,
    terminal_outcome,
    tool_call_sha256,
)
from app.services.harness.sessions.turn_budget import (
    TurnBudgetCharge,
    TurnBudgetExceeded,
    TurnBudgetSnapshot,
    charge_turn_budget,
)
from app.services.harness.sessions.turn_state_machine import (
    TurnEnginePhase,
    TurnEngineSignal,
    TurnEngineSnapshot,
    transition_turn,
)


class _ProviderStreamFailure(RuntimeError):
    pass


class SingleTurnEngine:
    def __init__(
        self,
        attempts: ProviderAttemptSource,
        tool_resolver: TurnToolResolver,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._attempts = attempts
        self._tool_resolver = tool_resolver
        self._clock = monotonic_clock

    async def run(self, budget: TurnBudgetSnapshot) -> SingleTurnOutcome:
        state = TurnEngineSnapshot(turn_id=budget.turn_id)
        events: list[TurnTraceEvent] = []
        state = append_lifecycle(
            state,
            TurnEngineSignal.START_CONTEXT,
            events,
        )
        state = append_lifecycle(
            state,
            TurnEngineSignal.CONTEXT_READY,
            events,
        )
        attempt = 0
        previous_time = self._read_clock()
        try:
            while True:
                attempt += 1
                budget, previous_time = self._charge_elapsed(
                    budget,
                    previous_time,
                )
                budget = charge_turn_budget(
                    budget,
                    TurnBudgetCharge(steps=1),
                )
                action = await self._consume_attempt(
                    attempt,
                    state,
                    budget,
                    events,
                    previous_time,
                )
                state, budget, previous_time, outcome = action
                if outcome is not None:
                    return outcome
                if state.phase is TurnEnginePhase.RETRY:
                    state = append_lifecycle(
                        state,
                        TurnEngineSignal.RETRY_READY,
                        events,
                    )
        except TurnBudgetExceeded as error:
            dimensions = ",".join(
                dimension.value for dimension in error.dimensions
            )
            return self._failed(
                state,
                budget,
                events,
                f"budget_exhausted:{dimensions}",
            )
        except TurnTraceLimit:
            if state.phase is not TurnEnginePhase.FAILED:
                state = transition_turn(state, TurnEngineSignal.FAIL)
            return terminal_outcome(
                state, budget, events, "turn_trace_limit"
            )
        except _ProviderStreamFailure:
            return self._failed(
                state,
                budget,
                events,
                "provider_stream_failed",
            )

    async def _consume_attempt(
        self,
        attempt: int,
        state: TurnEngineSnapshot,
        budget: TurnBudgetSnapshot,
        events: list[TurnTraceEvent],
        previous_time: float,
    ) -> tuple[
        TurnEngineSnapshot,
        TurnBudgetSnapshot,
        float,
        SingleTurnOutcome | None,
    ]:
        saw_event = False
        saw_usage = False
        saw_tool = False
        prior_usage: ProviderUsage | None = None
        try:
            stream = self._attempts.open_attempt(attempt)
            async for provider_event in stream:
                saw_event = True
                budget, previous_time = self._charge_elapsed(
                    budget,
                    previous_time,
                )
                budget = charge_turn_budget(
                    budget,
                    TurnBudgetCharge(
                        stream_bytes=len(
                            provider_event.model_dump_json().encode()
                        )
                    ),
                )
                if isinstance(provider_event, ProviderToolCall):
                    state, budget = await self._resolve_tool(
                        attempt,
                        state,
                        budget,
                        events,
                        provider_event,
                    )
                    saw_tool = True
                    continue
                append_provider(events, attempt, provider_event)
                if isinstance(provider_event, ProviderUsage):
                    try:
                        budget = charge_provider_usage(
                            budget,
                            prior_usage,
                            provider_event,
                        )
                    except ValueError as error:
                        raise _ProviderStreamFailure from error
                    prior_usage = provider_event
                    saw_usage = True
                    continue
                if isinstance(provider_event, ProviderError):
                    if provider_event.retry_allowed:
                        budget = charge_turn_budget(
                            budget,
                            TurnBudgetCharge(provider_retries=1),
                        )
                        state = append_lifecycle(
                            state,
                            TurnEngineSignal.RETRY_REQUIRED,
                            events,
                        )
                        return state, budget, previous_time, None
                    return (
                        state,
                        budget,
                        previous_time,
                        self._failed(
                            state,
                            budget,
                            events,
                            f"provider_{provider_event.failure_class.value}",
                        ),
                    )
                if isinstance(provider_event, ProviderCancelled):
                    state = append_lifecycle(
                        state,
                        TurnEngineSignal.CANCEL_REQUESTED,
                        events,
                    )
                    state = append_lifecycle(
                        state,
                        TurnEngineSignal.CANCEL_CONFIRMED,
                        events,
                    )
                    return (
                        state,
                        budget,
                        previous_time,
                        terminal_outcome(
                            state,
                            budget,
                            events,
                            "provider_cancelled",
                        ),
                    )
                if isinstance(provider_event, ProviderCompleted):
                    outcome = self._complete_attempt(
                        state,
                        budget,
                        events,
                        provider_event,
                        saw_usage,
                        saw_tool,
                    )
                    return state, budget, previous_time, outcome
        except (TurnBudgetExceeded, TurnTraceLimit):
            raise
        except Exception as error:
            raise _ProviderStreamFailure from error
        reason = (
            "provider_stream_empty"
            if not saw_event
            else "provider_stream_incomplete"
        )
        return (
            state,
            budget,
            previous_time,
            self._failed(state, budget, events, reason),
        )

    async def _resolve_tool(
        self,
        attempt: int,
        state: TurnEngineSnapshot,
        budget: TurnBudgetSnapshot,
        events: list[TurnTraceEvent],
        call: ProviderToolCall,
    ) -> tuple[TurnEngineSnapshot, TurnBudgetSnapshot]:
        require_trace_capacity(events, nonterminal_events=3)
        state = append_lifecycle(
            state,
            TurnEngineSignal.TOOL_REQUESTED,
            events,
        )
        budget = charge_turn_budget(
            budget,
            TurnBudgetCharge(
                tool_calls=1,
                call_sha256=tool_call_sha256(call),
            ),
        )
        try:
            resolution = await self._tool_resolver.resolve(call)
        except Exception:
            resolution = failed_tool_resolution()
        append_trace(
            events,
            TurnToolExchangeTrace(
                sequence=len(events) + 1,
                attempt=attempt,
                call=call,
                resolution=resolution,
            ),
        )
        budget = charge_turn_budget(
            budget,
            TurnBudgetCharge(
                tool_output_bytes=resolution.result_bytes
            ),
        )
        state = append_lifecycle(
            state,
            TurnEngineSignal.TOOL_COMPLETED,
            events,
        )
        return state, budget

    def _complete_attempt(
        self,
        state: TurnEngineSnapshot,
        budget: TurnBudgetSnapshot,
        events: list[TurnTraceEvent],
        completed: ProviderCompleted,
        saw_usage: bool,
        saw_tool: bool,
    ) -> SingleTurnOutcome | None:
        if completed.finish_reason is ProviderFinishReason.TOOL_CALLS:
            if saw_tool:
                return None
            return self._failed(
                state,
                budget,
                events,
                "provider_tool_result_missing",
            )
        if completed.finish_reason is ProviderFinishReason.LENGTH:
            return self._failed(
                state,
                budget,
                events,
                "provider_output_length",
            )
        if not saw_usage:
            return self._failed(
                state,
                budget,
                events,
                "provider_usage_missing",
            )
        state = append_lifecycle(
            state,
            TurnEngineSignal.COMPLETE,
            events,
        )
        return terminal_outcome(state, budget, events, "completed")

    def _charge_elapsed(
        self,
        budget: TurnBudgetSnapshot,
        previous_time: float,
    ) -> tuple[TurnBudgetSnapshot, float]:
        current_time = self._read_clock()
        if current_time < previous_time:
            raise _ProviderStreamFailure
        elapsed_ms = int((current_time - previous_time) * 1000)
        if elapsed_ms:
            budget = charge_turn_budget(
                budget,
                TurnBudgetCharge(duration_ms=elapsed_ms),
            )
        return budget, current_time

    def _read_clock(self) -> float:
        value = self._clock()
        if value < 0:
            raise ValueError("monotonic clock cannot be negative")
        return value

    def _failed(
        self,
        state: TurnEngineSnapshot,
        budget: TurnBudgetSnapshot,
        events: list[TurnTraceEvent],
        reason: str,
    ) -> SingleTurnOutcome:
        if state.phase is not TurnEnginePhase.FAILED:
            state = append_lifecycle(
                state,
                TurnEngineSignal.FAIL,
                events,
            )
        return terminal_outcome(state, budget, events, reason)
