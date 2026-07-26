"""Immutable authorization and behavior contract for every client command."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import Capability, StrictProtocolModel
from app.services.harness.protocol.event_vocabulary import CanonicalEventType


class CommandKind(StrEnum):
    WORKSPACE_OPEN = "workspace.open"
    WORKSPACE_CLOSE = "workspace.close"
    THREAD_CREATE = "thread.create"
    THREAD_RESUME = "thread.resume"
    THREAD_FORK = "thread.fork"
    THREAD_ARCHIVE = "thread.archive"
    THREAD_LIST = "thread.list"
    TURN_START = "turn.start"
    TURN_STEER = "turn.steer"
    TURN_CANCEL = "turn.cancel"
    TURN_COMPACT = "turn.compact"
    APPROVAL_RESPOND = "approval.respond"
    TASK_INSPECT = "task.inspect"
    TASK_CANCEL = "task.cancel"
    TASK_RETRY = "task.retry"
    CAPABILITY_LIST = "capability.list"
    EVENT_SUBSCRIBE = "event.subscribe"
    EVENT_ACKNOWLEDGE = "event.acknowledge"
    ARTIFACT_FETCH = "artifact.fetch"
    EVALUATION_START = "evaluation.start"
    EVALUATION_STATUS = "evaluation.status"


class IdempotencyRequirement(StrEnum):
    REQUIRED = "required"
    FORBIDDEN = "forbidden"


class SequencePrecondition(StrEnum):
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


class CommandResponseKind(StrEnum):
    WORKSPACE_STATUS = "workspace_status"
    THREAD_STATUS = "thread_status"
    THREAD_PAGE = "thread_page"
    TURN_STATUS = "turn_status"
    APPROVAL_STATUS = "approval_status"
    TASK_STATUS = "task_status"
    CAPABILITY_PAGE = "capability_page"
    EVENT_SUBSCRIPTION = "event_subscription"
    EVENT_ACKNOWLEDGEMENT = "event_acknowledgement"
    ARTIFACT_RANGE = "artifact_range"
    EVALUATION_STATUS = "evaluation_status"


class CommandContract(StrictProtocolModel):
    kind: CommandKind
    authorization_capability: Capability
    idempotency: IdempotencyRequirement
    sequence_precondition: SequencePrecondition
    response: CommandResponseKind
    emitted_events: tuple[CanonicalEventType, ...] = Field(max_length=16)
    reauthorize_on_execution: bool

    @model_validator(mode="after")
    def validate_emitted_events(self) -> Self:
        if tuple(sorted(set(self.emitted_events))) != self.emitted_events:
            raise ValueError("emitted event types must be unique and sorted")
        return self


def _contract(
    kind: CommandKind,
    capability: Capability,
    response: CommandResponseKind,
    emitted_events: tuple[CanonicalEventType, ...] = (),
    *,
    mutating: bool,
    reauthorize: bool = False,
) -> CommandContract:
    idempotency = (
        IdempotencyRequirement.REQUIRED
        if mutating
        else IdempotencyRequirement.FORBIDDEN
    )
    sequence_precondition = (
        SequencePrecondition.OPTIONAL
        if mutating
        else SequencePrecondition.FORBIDDEN
    )
    return CommandContract(
        kind=kind,
        authorization_capability=capability,
        idempotency=idempotency,
        sequence_precondition=sequence_precondition,
        response=response,
        emitted_events=emitted_events,
        reauthorize_on_execution=reauthorize,
    )


COMMAND_CONTRACTS = (
    _contract(
        CommandKind.WORKSPACE_OPEN,
        "workspace.open",
        CommandResponseKind.WORKSPACE_STATUS,
        (CanonicalEventType.WORKSPACE_OPENED,),
        mutating=True,
    ),
    _contract(
        CommandKind.WORKSPACE_CLOSE,
        "workspace.close",
        CommandResponseKind.WORKSPACE_STATUS,
        (CanonicalEventType.WORKSPACE_CLOSED,),
        mutating=True,
    ),
    _contract(
        CommandKind.THREAD_CREATE,
        "thread.create",
        CommandResponseKind.THREAD_STATUS,
        (CanonicalEventType.THREAD_CREATED,),
        mutating=True,
    ),
    _contract(
        CommandKind.THREAD_RESUME,
        "thread.resume",
        CommandResponseKind.THREAD_STATUS,
        (CanonicalEventType.THREAD_RESUMED,),
        mutating=True,
    ),
    _contract(
        CommandKind.THREAD_FORK,
        "thread.fork",
        CommandResponseKind.THREAD_STATUS,
        (CanonicalEventType.THREAD_FORKED,),
        mutating=True,
    ),
    _contract(
        CommandKind.THREAD_ARCHIVE,
        "thread.archive",
        CommandResponseKind.THREAD_STATUS,
        (CanonicalEventType.THREAD_ARCHIVED,),
        mutating=True,
    ),
    _contract(
        CommandKind.THREAD_LIST,
        "thread.list",
        CommandResponseKind.THREAD_PAGE,
        mutating=False,
    ),
    _contract(
        CommandKind.TURN_START,
        "turn.start",
        CommandResponseKind.TURN_STATUS,
        (CanonicalEventType.TURN_ACCEPTED,),
        mutating=True,
    ),
    _contract(
        CommandKind.TURN_STEER,
        "turn.steer",
        CommandResponseKind.TURN_STATUS,
        (CanonicalEventType.TURN_STEERED,),
        mutating=True,
    ),
    _contract(
        CommandKind.TURN_CANCEL,
        "turn.cancel",
        CommandResponseKind.TURN_STATUS,
        (CanonicalEventType.TURN_CANCELLATION_REQUESTED,),
        mutating=True,
    ),
    _contract(
        CommandKind.TURN_COMPACT,
        "turn.compact",
        CommandResponseKind.TURN_STATUS,
        (CanonicalEventType.TURN_COMPACTION_REQUESTED,),
        mutating=True,
    ),
    _contract(
        CommandKind.APPROVAL_RESPOND,
        "approval.respond",
        CommandResponseKind.APPROVAL_STATUS,
        (
            CanonicalEventType.APPROVAL_APPROVED,
            CanonicalEventType.APPROVAL_DENIED,
        ),
        mutating=True,
        reauthorize=True,
    ),
    _contract(
        CommandKind.TASK_INSPECT,
        "task.inspect",
        CommandResponseKind.TASK_STATUS,
        mutating=False,
    ),
    _contract(
        CommandKind.TASK_CANCEL,
        "task.cancel",
        CommandResponseKind.TASK_STATUS,
        (CanonicalEventType.TASK_CANCELLATION_REQUESTED,),
        mutating=True,
    ),
    _contract(
        CommandKind.TASK_RETRY,
        "task.retry",
        CommandResponseKind.TASK_STATUS,
        (CanonicalEventType.TASK_RETRIED,),
        mutating=True,
    ),
    _contract(
        CommandKind.CAPABILITY_LIST,
        "capability.list",
        CommandResponseKind.CAPABILITY_PAGE,
        mutating=False,
    ),
    _contract(
        CommandKind.EVENT_SUBSCRIBE,
        "event.subscribe",
        CommandResponseKind.EVENT_SUBSCRIPTION,
        (CanonicalEventType.EVENT_SUBSCRIPTION_STARTED,),
        mutating=False,
        reauthorize=True,
    ),
    _contract(
        CommandKind.EVENT_ACKNOWLEDGE,
        "event.acknowledge",
        CommandResponseKind.EVENT_ACKNOWLEDGEMENT,
        (CanonicalEventType.EVENT_ACKNOWLEDGED,),
        mutating=True,
        reauthorize=True,
    ),
    _contract(
        CommandKind.ARTIFACT_FETCH,
        "artifact.fetch",
        CommandResponseKind.ARTIFACT_RANGE,
        mutating=False,
    ),
    _contract(
        CommandKind.EVALUATION_START,
        "evaluation.start",
        CommandResponseKind.EVALUATION_STATUS,
        (CanonicalEventType.EVALUATION_ACCEPTED,),
        mutating=True,
    ),
    _contract(
        CommandKind.EVALUATION_STATUS,
        "evaluation.status",
        CommandResponseKind.EVALUATION_STATUS,
        mutating=False,
    ),
)

def _build_contract_index(
    contracts: tuple[CommandContract, ...],
) -> Mapping[CommandKind, CommandContract]:
    contract_kinds = tuple(contract.kind for contract in contracts)
    if len(set(contract_kinds)) != len(contract_kinds):
        raise RuntimeError("command contract registry contains duplicate kinds")
    if set(contract_kinds) != set(CommandKind):
        raise RuntimeError("command contract registry does not cover every kind")
    return MappingProxyType({contract.kind: contract for contract in contracts})


COMMAND_CONTRACT_BY_KIND = _build_contract_index(COMMAND_CONTRACTS)


def command_contract(kind: CommandKind) -> CommandContract:
    """Return the immutable execution contract for a validated command kind."""

    return COMMAND_CONTRACT_BY_KIND[kind]
