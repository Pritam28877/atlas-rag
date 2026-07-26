"""Durable thread, turn, item, and context-manifest records."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    ArtifactId,
    BoundedReason,
    ContextId,
    EventId,
    ExecutionBudget,
    IdempotencyKey,
    ItemId,
    Payload,
    PolicyVersion,
    ResourceUsage,
    Sha256,
    StrictProtocolModel,
    ThreadId,
    TraceLink,
    TurnId,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.states import ThreadState, TurnState

type AgentName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
type SourceId = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=256,
        pattern=r"^[a-z][A-Za-z0-9]*(?:[._:/-][A-Za-z0-9]+)*$",
    ),
]


class RetentionClass(StrEnum):
    EPHEMERAL = "ephemeral"
    STANDARD = "standard"
    COMPLIANCE = "compliance"


class ItemKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL = "approval"
    ARTIFACT = "artifact"
    ERROR = "error"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ContextSourceKind(StrEnum):
    POLICY = "policy"
    INSTRUCTION = "instruction"
    THREAD_TAIL = "thread_tail"
    TASK_ARTIFACT = "task_artifact"
    SUMMARY = "summary"
    MEMORY = "memory"
    DOCUMENT = "document"
    TOOL_CATALOG = "tool_catalog"


class ContextOmissionKind(StrEnum):
    TOKEN_BUDGET = "token_budget"
    POLICY_DENIED = "policy_denied"
    STALE = "stale"
    DUPLICATE = "duplicate"
    LOW_RELEVANCE = "low_relevance"
    UNSUPPORTED = "unsupported"


class ThreadRecord(StrictProtocolModel):
    thread_id: ThreadId
    workspace_id: WorkspaceId
    parent_thread_id: ThreadId | None = None
    fork_event_id: EventId | None = None
    state: ThreadState
    retention_class: RetentionClass
    event_sequence: int = Field(ge=0)
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_lineage_and_time(self) -> Self:
        has_parent = self.parent_thread_id is not None
        has_fork_event = self.fork_event_id is not None
        if has_parent != has_fork_event:
            raise ValueError("fork lineage requires parent_thread_id and fork_event_id")
        if self.parent_thread_id == self.thread_id:
            raise ValueError("thread cannot be its own parent")
        if self.updated_at < self.created_at:
            raise ValueError("thread updated_at cannot precede created_at")
        return self


_TERMINAL_TURN_STATES = {
    TurnState.COMPLETED,
    TurnState.FAILED,
    TurnState.CANCELLED,
}


class TurnRecord(StrictProtocolModel):
    turn_id: TurnId
    thread_id: ThreadId
    idempotency_key: IdempotencyKey
    requested_agent: AgentName
    provider_policy_version: PolicyVersion
    state: TurnState
    budget: ExecutionBudget
    usage: ResourceUsage
    accepted_at: UtcTimestamp
    deadline_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    completed_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        maximum_deadline = self.accepted_at + timedelta(
            milliseconds=self.budget.max_duration_ms
        )
        if not self.accepted_at < self.deadline_at <= maximum_deadline:
            raise ValueError("turn deadline must fit its duration budget")
        if self.started_at is not None and not (
            self.accepted_at <= self.started_at <= self.deadline_at
        ):
            raise ValueError("turn started_at must be inside its execution window")
        if self.state is TurnState.ACCEPTED and self.started_at is not None:
            raise ValueError("accepted turn cannot have started_at")
        if self.state not in {TurnState.ACCEPTED, TurnState.CANCELLED}:
            if self.started_at is None:
                raise ValueError("started turn state requires started_at")
        if self.state in _TERMINAL_TURN_STATES:
            if self.completed_at is None:
                raise ValueError("terminal turn requires completed_at")
        elif self.completed_at is not None:
            raise ValueError("non-terminal turn cannot have completed_at")
        if self.completed_at is not None:
            earliest_completion = self.started_at or self.accepted_at
            if self.completed_at < earliest_completion:
                raise ValueError("turn completed_at precedes execution")
        exceeded = self.usage.exceeds(self.budget)
        if exceeded:
            raise ValueError(f"turn usage exceeds budget: {', '.join(exceeded)}")
        return self


class ItemRecord(StrictProtocolModel):
    item_id: ItemId
    thread_id: ThreadId
    turn_id: TurnId
    ordinal: int = Field(ge=0, le=1_000_000)
    kind: ItemKind
    classification: DataClassification
    payload: Payload
    created_at: UtcTimestamp
    trace: TraceLink
    redacted: bool = False


class ContextSourceReference(StrictProtocolModel):
    source_id: SourceId
    kind: ContextSourceKind
    content_sha256: Sha256
    token_count: int = Field(ge=0, le=2_000_000)
    priority: int = Field(ge=0, le=1_000_000)
    artifact_id: ArtifactId | None = None
    event_id: EventId | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.artifact_id is not None and self.event_id is not None:
            message = "context source cannot reference artifact and event together"
            raise ValueError(message)
        return self


class ContextOmission(StrictProtocolModel):
    source_id: SourceId
    kind: ContextOmissionKind
    reason: BoundedReason


class ContextManifest(StrictProtocolModel):
    context_id: ContextId
    turn_id: TurnId
    provider_policy_version: PolicyVersion
    context_window_tokens: int = Field(ge=1, le=2_000_000)
    selected_token_count: int = Field(ge=0, le=2_000_000)
    reserved_output_tokens: int = Field(ge=1, le=512_000)
    reserved_reasoning_tokens: int = Field(ge=0, le=512_000)
    reserved_tool_tokens: int = Field(ge=0, le=512_000)
    sources: tuple[ContextSourceReference, ...] = Field(max_length=512)
    omissions: tuple[ContextOmission, ...] = Field(max_length=512)
    compiled_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_token_accounting(self) -> Self:
        source_ids = tuple(source.source_id for source in self.sources)
        if tuple(dict.fromkeys(source_ids)) != source_ids:
            raise ValueError("context source IDs must be unique")
        omission_ids = tuple(omission.source_id for omission in self.omissions)
        if tuple(dict.fromkeys(omission_ids)) != omission_ids:
            raise ValueError("context omission IDs must be unique")
        selected_total = sum(source.token_count for source in self.sources)
        if selected_total != self.selected_token_count:
            raise ValueError("selected token count must equal source token counts")
        reserved_total = (
            self.reserved_output_tokens
            + self.reserved_reasoning_tokens
            + self.reserved_tool_tokens
        )
        if self.selected_token_count + reserved_total > self.context_window_tokens:
            raise ValueError("selected and reserved tokens exceed context window")
        return self
