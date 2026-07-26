"""Typed contracts and atomic trace evidence for one deterministic turn."""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ProviderStreamEvent,
    ProviderToolCall,
    Sha256,
    StrictProtocolModel,
    TurnId,
)
from app.services.harness.protocol.base import BoundedReason
from app.services.harness.sessions.turn_budget import TurnBudgetSnapshot
from app.services.harness.sessions.turn_state_machine import (
    TurnEnginePhase,
    TurnEngineSignal,
)

MAXIMUM_TURN_TRACE_EVENTS = 10_000


class ToolResolutionStatus(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    FAILED = "failed"


class ToolResolution(StrictProtocolModel):
    status: ToolResolutionStatus
    result_sha256: Sha256
    result_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    durable: Literal[True] = True
    reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if (self.status is ToolResolutionStatus.ALLOWED) == (
            self.reason is None
        ):
            return self
        raise ValueError("denied or failed tool resolution requires a reason")


class ProviderAttemptSource(Protocol):
    def open_attempt(
        self,
        attempt: int,
    ) -> AsyncIterator[ProviderStreamEvent]: ...


class TurnToolResolver(Protocol):
    async def resolve(self, call: ProviderToolCall) -> ToolResolution: ...


class TurnTraceKind(StrEnum):
    LIFECYCLE = "lifecycle"
    PROVIDER = "provider"
    TOOL_EXCHANGE = "tool_exchange"
    TERMINAL = "terminal"


class TurnLifecycleTrace(StrictProtocolModel):
    kind: Literal[TurnTraceKind.LIFECYCLE] = TurnTraceKind.LIFECYCLE
    sequence: int = Field(ge=1, le=MAXIMUM_TURN_TRACE_EVENTS)
    phase: TurnEnginePhase
    signal: TurnEngineSignal


class TurnProviderTrace(StrictProtocolModel):
    kind: Literal[TurnTraceKind.PROVIDER] = TurnTraceKind.PROVIDER
    sequence: int = Field(ge=1, le=MAXIMUM_TURN_TRACE_EVENTS)
    attempt: int = Field(ge=1, le=256)
    event: ProviderStreamEvent

    @model_validator(mode="after")
    def reject_unpaired_tool_call(self) -> Self:
        if isinstance(self.event, ProviderToolCall):
            raise ValueError("tool call must use an atomic tool exchange")
        return self


class TurnToolExchangeTrace(StrictProtocolModel):
    kind: Literal[TurnTraceKind.TOOL_EXCHANGE] = TurnTraceKind.TOOL_EXCHANGE
    sequence: int = Field(ge=1, le=MAXIMUM_TURN_TRACE_EVENTS)
    attempt: int = Field(ge=1, le=256)
    call: ProviderToolCall
    resolution: ToolResolution


class TurnTerminalTrace(StrictProtocolModel):
    kind: Literal[TurnTraceKind.TERMINAL] = TurnTraceKind.TERMINAL
    sequence: int = Field(ge=1, le=MAXIMUM_TURN_TRACE_EVENTS)
    phase: TurnEnginePhase
    reason: BoundedReason
    budget: TurnBudgetSnapshot

    @model_validator(mode="after")
    def validate_terminal_phase(self) -> Self:
        if self.phase not in {
            TurnEnginePhase.COMPLETED,
            TurnEnginePhase.FAILED,
            TurnEnginePhase.CANCELLED,
        }:
            raise ValueError("terminal trace requires a terminal phase")
        return self


type TurnTraceEvent = Annotated[
    TurnLifecycleTrace
    | TurnProviderTrace
    | TurnToolExchangeTrace
    | TurnTerminalTrace,
    Field(discriminator="kind"),
]


class SingleTurnOutcome(StrictProtocolModel):
    turn_id: TurnId
    phase: TurnEnginePhase
    budget: TurnBudgetSnapshot
    events: tuple[TurnTraceEvent, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_TURN_TRACE_EVENTS,
    )

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> Self:
        expected_sequences = tuple(range(1, len(self.events) + 1))
        actual_sequences = tuple(event.sequence for event in self.events)
        if actual_sequences != expected_sequences:
            raise ValueError("turn trace sequence must be contiguous")
        terminal_events = tuple(
            event
            for event in self.events
            if isinstance(event, TurnTerminalTrace)
        )
        if len(terminal_events) != 1 or self.events[-1] != terminal_events[0]:
            raise ValueError("turn outcome requires exactly one final terminal event")
        terminal = terminal_events[0]
        if (
            self.phase is not terminal.phase
            or self.budget != terminal.budget
            or self.turn_id != self.budget.turn_id
        ):
            raise ValueError("turn terminal evidence disagrees with outcome")
        return self
