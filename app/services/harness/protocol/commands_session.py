"""Workspace, thread, turn, and approval command vocabulary."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ApprovalId,
    BoundedReason,
    EventId,
    ExecutionBudget,
    PageRequest,
    Payload,
    Sha256,
    ThreadId,
    TurnId,
)
from app.services.harness.protocol.command_base import MutatingCommand, QueryCommand
from app.services.harness.protocol.conversation import (
    AgentName,
    DataClassification,
    RetentionClass,
)
from app.services.harness.protocol.states import ApprovalState, ThreadState


class WorkspaceAccessMode(StrEnum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class WorkspaceOpenCommand(MutatingCommand):
    kind: Literal["workspace.open"] = "workspace.open"
    access_mode: WorkspaceAccessMode
    repository_fingerprint_sha256: Sha256


class WorkspaceCloseCommand(MutatingCommand):
    kind: Literal["workspace.close"] = "workspace.close"
    reason: BoundedReason


class ThreadCreateCommand(MutatingCommand):
    kind: Literal["thread.create"] = "thread.create"
    retention_class: RetentionClass


class ThreadResumeCommand(MutatingCommand):
    kind: Literal["thread.resume"] = "thread.resume"
    thread_id: ThreadId


class ThreadForkCommand(MutatingCommand):
    kind: Literal["thread.fork"] = "thread.fork"
    source_thread_id: ThreadId
    fork_event_id: EventId
    retention_class: RetentionClass


class ThreadArchiveCommand(MutatingCommand):
    kind: Literal["thread.archive"] = "thread.archive"
    thread_id: ThreadId
    reason: BoundedReason


class ThreadListCommand(QueryCommand):
    kind: Literal["thread.list"] = "thread.list"
    page: PageRequest = Field(default_factory=PageRequest)
    states: tuple[ThreadState, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_state_filter(self) -> Self:
        if tuple(sorted(set(self.states))) != self.states:
            raise ValueError("thread states must be unique and sorted")
        return self


class TurnStartCommand(MutatingCommand):
    kind: Literal["turn.start"] = "turn.start"
    thread_id: ThreadId
    requested_agent: AgentName
    budget: ExecutionBudget
    classification: DataClassification
    initial_payload: Payload


class TurnSteerCommand(MutatingCommand):
    kind: Literal["turn.steer"] = "turn.steer"
    turn_id: TurnId
    classification: DataClassification
    payload: Payload


class TurnCancelCommand(MutatingCommand):
    kind: Literal["turn.cancel"] = "turn.cancel"
    turn_id: TurnId
    reason: BoundedReason


class TurnCompactCommand(MutatingCommand):
    kind: Literal["turn.compact"] = "turn.compact"
    turn_id: TurnId
    target_input_tokens: int = Field(ge=1, le=2_000_000)
    reason: BoundedReason


class ApprovalRespondCommand(MutatingCommand):
    kind: Literal["approval.respond"] = "approval.respond"
    approval_id: ApprovalId
    request_sha256: Sha256
    decision: ApprovalState
    reason: BoundedReason

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.decision not in {ApprovalState.APPROVED, ApprovalState.DENIED}:
            raise ValueError("approval response must be approved or denied")
        return self


type SessionCommandTypes = (
    WorkspaceOpenCommand
    | WorkspaceCloseCommand
    | ThreadCreateCommand
    | ThreadResumeCommand
    | ThreadForkCommand
    | ThreadArchiveCommand
    | ThreadListCommand
    | TurnStartCommand
    | TurnSteerCommand
    | TurnCancelCommand
    | TurnCompactCommand
    | ApprovalRespondCommand
)
SessionCommand = Annotated[SessionCommandTypes, Field(discriminator="kind")]
