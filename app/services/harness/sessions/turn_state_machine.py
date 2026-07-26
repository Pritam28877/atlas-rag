"""Closed deterministic control graph for one Atlas Harness turn."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import Field, model_validator

from app.services.harness.protocol import StrictProtocolModel, TurnId


class TurnEnginePhase(StrEnum):
    ACCEPTED = "accepted"
    CONTEXT = "context"
    MODEL = "model"
    APPROVAL = "approval"
    TOOL = "tool"
    RETRY = "retry"
    CANCELLING = "cancelling"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnEngineSignal(StrEnum):
    START_CONTEXT = "start_context"
    CONTEXT_READY = "context_ready"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    TOOL_REQUESTED = "tool_requested"
    TOOL_COMPLETED = "tool_completed"
    RETRY_REQUIRED = "retry_required"
    RETRY_READY = "retry_ready"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_CONFIRMED = "cancel_confirmed"
    RECONCILE_REQUIRED = "reconcile_required"
    RECONCILE_RESUMED = "reconcile_resumed"
    COMPLETE = "complete"
    FAIL = "fail"


class TurnEngineTransitionError(ValueError):
    def __init__(
        self,
        phase: TurnEnginePhase,
        signal: TurnEngineSignal,
    ) -> None:
        super().__init__(f"invalid turn engine transition: {phase} + {signal}")
        self.phase = phase
        self.signal = signal


class TurnEngineSnapshot(StrictProtocolModel):
    turn_id: TurnId
    phase: TurnEnginePhase = TurnEnginePhase.ACCEPTED
    transition_sequence: int = Field(default=0, ge=0, le=1_000_000)
    last_signal: TurnEngineSignal | None = None

    @model_validator(mode="after")
    def validate_transition_evidence(self) -> TurnEngineSnapshot:
        initial = self.transition_sequence == 0
        if initial != (self.last_signal is None):
            raise ValueError("transition sequence and last signal disagree")
        if initial and self.phase is not TurnEnginePhase.ACCEPTED:
            raise ValueError("initial engine phase must be accepted")
        return self


_TRANSITIONS: Mapping[
    TurnEnginePhase,
    Mapping[TurnEngineSignal, TurnEnginePhase],
] = {
    TurnEnginePhase.ACCEPTED: {
        TurnEngineSignal.START_CONTEXT: TurnEnginePhase.CONTEXT,
        TurnEngineSignal.CANCEL_REQUESTED: TurnEnginePhase.CANCELLING,
        TurnEngineSignal.FAIL: TurnEnginePhase.FAILED,
    },
    TurnEnginePhase.CONTEXT: {
        TurnEngineSignal.CONTEXT_READY: TurnEnginePhase.MODEL,
        TurnEngineSignal.CANCEL_REQUESTED: TurnEnginePhase.CANCELLING,
        TurnEngineSignal.FAIL: TurnEnginePhase.FAILED,
    },
    TurnEnginePhase.MODEL: {
        TurnEngineSignal.APPROVAL_REQUIRED: TurnEnginePhase.APPROVAL,
        TurnEngineSignal.TOOL_REQUESTED: TurnEnginePhase.TOOL,
        TurnEngineSignal.RETRY_REQUIRED: TurnEnginePhase.RETRY,
        TurnEngineSignal.CANCEL_REQUESTED: TurnEnginePhase.CANCELLING,
        TurnEngineSignal.RECONCILE_REQUIRED: TurnEnginePhase.RECONCILING,
        TurnEngineSignal.COMPLETE: TurnEnginePhase.COMPLETED,
        TurnEngineSignal.FAIL: TurnEnginePhase.FAILED,
    },
    TurnEnginePhase.APPROVAL: {
        TurnEngineSignal.APPROVAL_GRANTED: TurnEnginePhase.TOOL,
        TurnEngineSignal.APPROVAL_DENIED: TurnEnginePhase.MODEL,
        TurnEngineSignal.CANCEL_REQUESTED: TurnEnginePhase.CANCELLING,
        TurnEngineSignal.FAIL: TurnEnginePhase.FAILED,
    },
    TurnEnginePhase.TOOL: {
        TurnEngineSignal.TOOL_COMPLETED: TurnEnginePhase.MODEL,
        TurnEngineSignal.RETRY_REQUIRED: TurnEnginePhase.RETRY,
        TurnEngineSignal.CANCEL_REQUESTED: TurnEnginePhase.CANCELLING,
        TurnEngineSignal.RECONCILE_REQUIRED: TurnEnginePhase.RECONCILING,
        TurnEngineSignal.FAIL: TurnEnginePhase.FAILED,
    },
    TurnEnginePhase.RETRY: {
        TurnEngineSignal.RETRY_READY: TurnEnginePhase.MODEL,
        TurnEngineSignal.CANCEL_REQUESTED: TurnEnginePhase.CANCELLING,
        TurnEngineSignal.FAIL: TurnEnginePhase.FAILED,
    },
    TurnEnginePhase.CANCELLING: {
        TurnEngineSignal.CANCEL_CONFIRMED: TurnEnginePhase.CANCELLED,
        TurnEngineSignal.RECONCILE_REQUIRED: TurnEnginePhase.RECONCILING,
        TurnEngineSignal.FAIL: TurnEnginePhase.FAILED,
    },
    TurnEnginePhase.RECONCILING: {
        TurnEngineSignal.RECONCILE_RESUMED: TurnEnginePhase.MODEL,
        TurnEngineSignal.CANCEL_CONFIRMED: TurnEnginePhase.CANCELLED,
        TurnEngineSignal.COMPLETE: TurnEnginePhase.COMPLETED,
        TurnEngineSignal.FAIL: TurnEnginePhase.FAILED,
    },
    TurnEnginePhase.COMPLETED: {},
    TurnEnginePhase.FAILED: {},
    TurnEnginePhase.CANCELLED: {},
}


def transition_turn(
    snapshot: TurnEngineSnapshot,
    signal: TurnEngineSignal,
) -> TurnEngineSnapshot:
    target = _TRANSITIONS[snapshot.phase].get(signal)
    if target is None:
        raise TurnEngineTransitionError(snapshot.phase, signal)
    return TurnEngineSnapshot(
        turn_id=snapshot.turn_id,
        phase=target,
        transition_sequence=snapshot.transition_sequence + 1,
        last_signal=signal,
    )


def allowed_turn_signals(
    phase: TurnEnginePhase,
) -> frozenset[TurnEngineSignal]:
    return frozenset(_TRANSITIONS[phase])
