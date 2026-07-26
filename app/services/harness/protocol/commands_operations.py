"""Task, catalog, event replay, artifact, and evaluation commands."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ArtifactId,
    BoundedReason,
    Capability,
    EvaluationId,
    ExecutionBudget,
    PageRequest,
    Sha256,
    SubscriptionId,
    TaskId,
)
from app.services.harness.protocol.command_base import MutatingCommand, QueryCommand
from app.services.harness.protocol.conversation import DataClassification
from app.services.harness.protocol.execution import EventType


class TaskInspectCommand(QueryCommand):
    kind: Literal["task.inspect"] = "task.inspect"
    task_id: TaskId


class TaskCancelCommand(MutatingCommand):
    kind: Literal["task.cancel"] = "task.cancel"
    task_id: TaskId
    reason: BoundedReason


class TaskRetryCommand(MutatingCommand):
    kind: Literal["task.retry"] = "task.retry"
    task_id: TaskId
    failed_attempt: int = Field(ge=1, le=16)
    task_spec_sha256: Sha256
    reason: BoundedReason


class CapabilityCatalog(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    MCP = "mcp"


class CapabilityListCommand(QueryCommand):
    kind: Literal["capability.list"] = "capability.list"
    catalog: CapabilityCatalog
    required_capabilities: tuple[Capability, ...] = Field(max_length=64)
    page: PageRequest = Field(default_factory=PageRequest)

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        values = self.required_capabilities
        if tuple(sorted(set(values))) != values:
            raise ValueError("required capabilities must be unique and sorted")
        return self


class EventSubscribeCommand(QueryCommand):
    kind: Literal["event.subscribe"] = "event.subscribe"
    after_sequence: int = Field(ge=0, le=2**63 - 1)
    max_batch_size: int = Field(default=256, ge=1, le=256)
    heartbeat_interval_ms: int = Field(default=15_000, ge=1_000, le=60_000)
    event_types: tuple[EventType, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_event_types(self) -> Self:
        if tuple(sorted(set(self.event_types))) != self.event_types:
            raise ValueError("event types must be unique and sorted")
        return self


class EventAcknowledgeCommand(MutatingCommand):
    kind: Literal["event.acknowledge"] = "event.acknowledge"
    subscription_id: SubscriptionId
    through_sequence: int = Field(ge=1, le=2**63 - 1)


class ArtifactFetchCommand(QueryCommand):
    kind: Literal["artifact.fetch"] = "artifact.fetch"
    artifact_id: ArtifactId
    expected_content_sha256: Sha256
    offset_bytes: int = Field(default=0, ge=0, le=4 * 1024 * 1024 * 1024 - 1)
    length_bytes: int = Field(default=1024 * 1024, ge=1, le=16 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        maximum_size = 4 * 1024 * 1024 * 1024
        if self.offset_bytes + self.length_bytes > maximum_size:
            raise ValueError("artifact byte range exceeds maximum artifact size")
        return self


class EvaluationStartCommand(MutatingCommand):
    kind: Literal["evaluation.start"] = "evaluation.start"
    fixture_sha256s: tuple[Sha256, ...] = Field(min_length=1, max_length=1000)
    configuration_sha256: Sha256
    classification: DataClassification
    budget: ExecutionBudget
    max_parallelism: int = Field(default=4, ge=1, le=64)

    @model_validator(mode="after")
    def validate_fixtures(self) -> Self:
        if tuple(sorted(set(self.fixture_sha256s))) != self.fixture_sha256s:
            raise ValueError("evaluation fixture hashes must be unique and sorted")
        return self


class EvaluationStatusCommand(QueryCommand):
    kind: Literal["evaluation.status"] = "evaluation.status"
    evaluation_id: EvaluationId


type OperationsCommandTypes = (
    TaskInspectCommand
    | TaskCancelCommand
    | TaskRetryCommand
    | CapabilityListCommand
    | EventSubscribeCommand
    | EventAcknowledgeCommand
    | ArtifactFetchCommand
    | EvaluationStartCommand
    | EvaluationStatusCommand
)
OperationsCommand = Annotated[
    OperationsCommandTypes,
    Field(discriminator="kind"),
]
