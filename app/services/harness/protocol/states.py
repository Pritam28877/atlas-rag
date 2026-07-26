"""Canonical lifecycle states and fail-closed transition validation."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class GrantState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ThreadState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class TurnState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_TOOL = "waiting_tool"
    COMPACTING = "compacting"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationState(StrEnum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


class ApprovalState(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DecisionOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class InvalidTransitionError(ValueError):
    """Raised before an invalid lifecycle transition reaches persistence."""


_GRANT_TRANSITIONS: Mapping[GrantState, frozenset[GrantState]] = {
    GrantState.ACTIVE: frozenset({GrantState.REVOKED, GrantState.EXPIRED}),
    GrantState.REVOKED: frozenset(),
    GrantState.EXPIRED: frozenset(),
}
_THREAD_TRANSITIONS: Mapping[ThreadState, frozenset[ThreadState]] = {
    ThreadState.ACTIVE: frozenset({ThreadState.ARCHIVED, ThreadState.DELETED}),
    ThreadState.ARCHIVED: frozenset({ThreadState.ACTIVE, ThreadState.DELETED}),
    ThreadState.DELETED: frozenset(),
}
_TURN_TRANSITIONS: Mapping[TurnState, frozenset[TurnState]] = {
    TurnState.ACCEPTED: frozenset(
        {TurnState.RUNNING, TurnState.FAILED, TurnState.CANCELLED}
    ),
    TurnState.RUNNING: frozenset(
        {
            TurnState.WAITING_APPROVAL,
            TurnState.WAITING_TOOL,
            TurnState.COMPACTING,
            TurnState.CANCELLING,
            TurnState.COMPLETED,
            TurnState.FAILED,
        }
    ),
    TurnState.WAITING_APPROVAL: frozenset(
        {TurnState.RUNNING, TurnState.CANCELLING, TurnState.FAILED}
    ),
    TurnState.WAITING_TOOL: frozenset(
        {TurnState.RUNNING, TurnState.CANCELLING, TurnState.FAILED}
    ),
    TurnState.COMPACTING: frozenset(
        {TurnState.RUNNING, TurnState.CANCELLING, TurnState.FAILED}
    ),
    TurnState.CANCELLING: frozenset({TurnState.CANCELLED, TurnState.FAILED}),
    TurnState.COMPLETED: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.CANCELLED: frozenset(),
}
_OPERATION_TRANSITIONS: Mapping[OperationState, frozenset[OperationState]] = {
    OperationState.PREPARED: frozenset(
        {
            OperationState.DISPATCHED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
    OperationState.DISPATCHED: frozenset(
        {
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.AMBIGUOUS,
            OperationState.CANCELLED,
        }
    ),
    OperationState.AMBIGUOUS: frozenset(
        {OperationState.COMPLETED, OperationState.FAILED}
    ),
    OperationState.COMPLETED: frozenset(),
    OperationState.FAILED: frozenset(),
    OperationState.CANCELLED: frozenset(),
}
_APPROVAL_TRANSITIONS: Mapping[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.REQUESTED: frozenset(
        {
            ApprovalState.APPROVED,
            ApprovalState.DENIED,
            ApprovalState.EXPIRED,
            ApprovalState.CANCELLED,
        }
    ),
    ApprovalState.APPROVED: frozenset(),
    ApprovalState.DENIED: frozenset(),
    ApprovalState.EXPIRED: frozenset(),
    ApprovalState.CANCELLED: frozenset(),
}
_TASK_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.READY: frozenset(
        {TaskState.RUNNING, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.CANCELLING,
        }
    ),
    TaskState.BLOCKED: frozenset(
        {TaskState.READY, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.CANCELLING: frozenset({TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def _require_transition[State: StrEnum](
    transitions: Mapping[State, frozenset[State]],
    current: State,
    target: State,
) -> None:
    if target not in transitions[current]:
        raise InvalidTransitionError(
            f"invalid {type(current).__name__} transition: {current} -> {target}"
        )


def require_grant_transition(current: GrantState, target: GrantState) -> None:
    _require_transition(_GRANT_TRANSITIONS, current, target)


def require_thread_transition(current: ThreadState, target: ThreadState) -> None:
    _require_transition(_THREAD_TRANSITIONS, current, target)


def require_turn_transition(current: TurnState, target: TurnState) -> None:
    _require_transition(_TURN_TRANSITIONS, current, target)


def require_operation_transition(
    current: OperationState,
    target: OperationState,
) -> None:
    _require_transition(_OPERATION_TRANSITIONS, current, target)


def require_approval_transition(
    current: ApprovalState,
    target: ApprovalState,
) -> None:
    _require_transition(_APPROVAL_TRANSITIONS, current, target)


def require_task_transition(current: TaskState, target: TaskState) -> None:
    _require_transition(_TASK_TRANSITIONS, current, target)
