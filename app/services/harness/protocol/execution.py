"""Auditable event, privileged operation, and approval records."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    AggregateId,
    ApprovalId,
    ArtifactId,
    BoundedLabel,
    BoundedReason,
    Capability,
    DecisionId,
    EventId,
    GrantId,
    IdempotencyKey,
    OperationId,
    Payload,
    PolicyVersion,
    PrincipalId,
    SchemaVersion,
    Sha256,
    StrictProtocolModel,
    TaskId,
    TraceLink,
    TurnId,
    UtcTimestamp,
)
from app.services.harness.protocol.states import ApprovalState, OperationState

type EventType = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Z][A-Za-z0-9]+(?:\.[A-Z][A-Za-z0-9]+)*$",
    ),
]
type ToolName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
type ToolVersion = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$",
    ),
]


class EventActorKind(StrEnum):
    AUTHENTICATED = "authenticated"
    SYSTEM = "system"


class EventRecord(StrictProtocolModel):
    """Append-only aggregate fact with complete actor and causation evidence."""

    event_id: EventId
    event_type: EventType
    schema_version: SchemaVersion
    aggregate_id: AggregateId
    aggregate_sequence: int = Field(ge=1)
    actor_kind: EventActorKind
    actor_principal_id: PrincipalId
    grant_id: GrantId | None = None
    policy_decision_id: DecisionId | None = None
    occurred_at: UtcTimestamp
    trace: TraceLink
    payload: Payload

    @model_validator(mode="after")
    def validate_authority_evidence(self) -> Self:
        has_grant = self.grant_id is not None
        has_decision = self.policy_decision_id is not None
        if has_grant != has_decision:
            raise ValueError("event grant and policy decision evidence must be paired")
        if self.actor_kind is EventActorKind.AUTHENTICATED and not has_grant:
            raise ValueError("authenticated event requires authorization evidence")
        if self.actor_kind is EventActorKind.SYSTEM and has_grant:
            raise ValueError("system event cannot claim a user grant")
        return self


class IdempotencyClass(StrEnum):
    READ_ONLY = "read_only"
    REPEATABLE = "repeatable"
    NON_IDEMPOTENT = "non_idempotent"


class OperationLimits(StrictProtocolModel):
    max_duration_ms: int = Field(ge=100, le=3_600_000)
    max_cpu_ms: int = Field(ge=100, le=3_600_000)
    max_memory_bytes: int = Field(ge=16 * 1024 * 1024, le=64 * 1024**3)
    max_output_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    max_processes: int = Field(ge=1, le=1024)


class OperationRecord(StrictProtocolModel):
    """Fenced side effect whose ambiguous state is never treated as failure."""

    operation_id: OperationId
    turn_id: TurnId
    task_id: TaskId | None = None
    idempotency_key: IdempotencyKey
    idempotency_class: IdempotencyClass
    attempt: int = Field(ge=1, le=16)
    lease_fencing_token: int = Field(ge=1)
    tool_name: ToolName
    tool_version: ToolVersion
    args_sha256: Sha256
    capability: Capability
    policy_decision_id: DecisionId
    approval_id: ApprovalId | None = None
    state: OperationState
    limits: OperationLimits
    prepared_at: UtcTimestamp
    dispatched_at: UtcTimestamp | None = None
    ambiguous_at: UtcTimestamp | None = None
    terminal_at: UtcTimestamp | None = None
    result_artifact_id: ArtifactId | None = None
    result_sha256: Sha256 | None = None
    status_reason: BoundedReason | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        requires_dispatch = self.state in {
            OperationState.DISPATCHED,
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.AMBIGUOUS,
        }
        if requires_dispatch and self.dispatched_at is None:
            raise ValueError("dispatched state and dispatched_at must agree")
        if self.state is OperationState.PREPARED and self.dispatched_at is not None:
            raise ValueError("dispatched state and dispatched_at must agree")
        if self.dispatched_at is not None and self.dispatched_at < self.prepared_at:
            raise ValueError("operation dispatch cannot precede preparation")

        is_ambiguous = self.state is OperationState.AMBIGUOUS
        if is_ambiguous != (self.ambiguous_at is not None):
            raise ValueError("ambiguous state and ambiguous_at must agree")
        if is_ambiguous and self.status_reason is None:
            raise ValueError("ambiguous operation requires a status reason")
        if self.ambiguous_at is not None and self.dispatched_at is not None:
            if self.ambiguous_at < self.dispatched_at:
                raise ValueError("ambiguity cannot precede dispatch")

        terminal_states = {
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
        is_terminal = self.state in terminal_states
        if is_terminal != (self.terminal_at is not None):
            raise ValueError("terminal state and terminal_at must agree")
        if self.terminal_at is not None and self.dispatched_at is not None:
            if self.terminal_at < self.dispatched_at:
                raise ValueError("operation terminal time cannot precede dispatch")
        self._validate_outcome_fields()
        return self

    def _validate_outcome_fields(self) -> None:
        if self.state is OperationState.COMPLETED:
            if self.result_sha256 is None or self.status_reason is not None:
                raise ValueError("completed operation requires only a result hash")
        elif self.state in {OperationState.FAILED, OperationState.CANCELLED}:
            if (
                self.status_reason is None
                or self.result_sha256 is not None
                or self.result_artifact_id is not None
            ):
                raise ValueError("failed operation requires only a status reason")
        elif self.result_sha256 is not None or self.result_artifact_id is not None:
            message = "non-completed operation cannot contain a result reference"
            raise ValueError(message)
        elif self.state is not OperationState.AMBIGUOUS:
            if self.status_reason is not None:
                raise ValueError("active operation cannot contain a status reason")


class ApprovalScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    WORKSPACE = "workspace"


class ApprovalRecord(StrictProtocolModel):
    """Hash-bound approval that narrows authority for one exact operation."""

    approval_id: ApprovalId
    operation_id: OperationId
    request_sha256: Sha256
    capability: Capability
    scope: ApprovalScope
    state: ApprovalState
    policy_version: PolicyVersion
    matched_rule_ids: tuple[BoundedLabel, ...] = Field(min_length=1, max_length=64)
    requested_at: UtcTimestamp
    decided_at: UtcTimestamp | None = None
    decided_by_principal_id: PrincipalId | None = None
    expires_at: UtcTimestamp
    reason: BoundedReason

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if tuple(sorted(set(self.matched_rule_ids))) != self.matched_rule_ids:
            raise ValueError("approval rule IDs must be unique and sorted")
        if self.expires_at <= self.requested_at:
            raise ValueError("approval expiry must follow request time")
        is_requested = self.state is ApprovalState.REQUESTED
        if is_requested == (self.decided_at is not None):
            raise ValueError("terminal approval state requires decided_at")
        human_decision = self.state in {
            ApprovalState.APPROVED,
            ApprovalState.DENIED,
        }
        if human_decision != (self.decided_by_principal_id is not None):
            raise ValueError("approved or denied state requires a deciding principal")
        if self.decided_at is not None:
            if not self.requested_at <= self.decided_at <= self.expires_at:
                raise ValueError("approval decision must be inside its validity window")
        return self
