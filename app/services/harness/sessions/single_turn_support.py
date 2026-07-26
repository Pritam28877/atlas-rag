"""Pure bounded helpers shared by deterministic single-turn execution."""

from __future__ import annotations

import hashlib

from app.services.harness.protocol import (
    ProviderStreamEvent,
    ProviderToolCall,
    ProviderUsage,
)
from app.services.harness.sessions.single_turn_contracts import (
    MAXIMUM_TURN_TRACE_EVENTS,
    SingleTurnOutcome,
    ToolResolution,
    ToolResolutionStatus,
    TurnLifecycleTrace,
    TurnProviderTrace,
    TurnTerminalTrace,
    TurnTraceEvent,
)
from app.services.harness.sessions.turn_budget import (
    TurnBudgetCharge,
    TurnBudgetSnapshot,
    charge_turn_budget,
)
from app.services.harness.sessions.turn_state_machine import (
    TurnEngineSignal,
    TurnEngineSnapshot,
    transition_turn,
)


class TurnTraceLimit(RuntimeError):
    pass


def require_trace_capacity(
    events: list[TurnTraceEvent],
    *,
    nonterminal_events: int,
) -> None:
    if len(events) + nonterminal_events > MAXIMUM_TURN_TRACE_EVENTS - 1:
        raise TurnTraceLimit


def append_trace(
    events: list[TurnTraceEvent],
    event: TurnTraceEvent,
) -> None:
    require_trace_capacity(events, nonterminal_events=1)
    events.append(event)


def append_lifecycle(
    state: TurnEngineSnapshot,
    signal: TurnEngineSignal,
    events: list[TurnTraceEvent],
) -> TurnEngineSnapshot:
    updated = transition_turn(state, signal)
    append_trace(
        events,
        TurnLifecycleTrace(
            sequence=len(events) + 1,
            phase=updated.phase,
            signal=signal,
        ),
    )
    return updated


def append_provider(
    events: list[TurnTraceEvent],
    attempt: int,
    event: ProviderStreamEvent,
) -> None:
    append_trace(
        events,
        TurnProviderTrace(
            sequence=len(events) + 1,
            attempt=attempt,
            event=event,
        ),
    )


def terminal_outcome(
    state: TurnEngineSnapshot,
    budget: TurnBudgetSnapshot,
    events: list[TurnTraceEvent],
    reason: str,
) -> SingleTurnOutcome:
    events.append(
        TurnTerminalTrace(
            sequence=len(events) + 1,
            phase=state.phase,
            reason=reason,
            budget=budget,
        )
    )
    return SingleTurnOutcome(
        turn_id=state.turn_id,
        phase=state.phase,
        budget=budget,
        events=tuple(events),
    )


def charge_provider_usage(
    budget: TurnBudgetSnapshot,
    prior: ProviderUsage | None,
    current: ProviderUsage,
) -> TurnBudgetSnapshot:
    prior_input = 0 if prior is None else prior.usage.input_tokens
    prior_output = (
        0
        if prior is None
        else prior.usage.output_tokens + prior.usage.reasoning_tokens
    )
    prior_cost = 0 if prior is None else prior.usage.cost_microusd
    input_tokens = current.usage.input_tokens - prior_input
    output_tokens = (
        current.usage.output_tokens
        + current.usage.reasoning_tokens
        - prior_output
    )
    cost_microusd = current.usage.cost_microusd - prior_cost
    if input_tokens < 0 or output_tokens < 0 or cost_microusd < 0:
        raise ValueError("provider usage must be cumulative")
    if input_tokens == output_tokens == cost_microusd == 0:
        return budget
    return charge_turn_budget(
        budget,
        TurnBudgetCharge(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
        ),
    )


def tool_call_sha256(call: ProviderToolCall) -> str:
    normalized_call = f"{call.tool_name}\0{call.arguments_json}".encode()
    return hashlib.sha256(normalized_call).hexdigest()


def failed_tool_resolution() -> ToolResolution:
    value = b'{"error":"tool_resolution_failed"}'
    return ToolResolution(
        status=ToolResolutionStatus.FAILED,
        result_sha256=hashlib.sha256(value).hexdigest(),
        result_bytes=len(value),
        reason="Tool resolution failed.",
    )
