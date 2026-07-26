"""Typed, bounded contracts for deterministic journal projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, TypeVar

from pydantic import (
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from app.services.harness.journal.contracts import JournalEvent
from app.services.harness.protocol import (
    SchemaVersion,
    Sha256,
    StrictProtocolModel,
    WorkspaceId,
)

MAXIMUM_PROJECTION_STATE_BYTES = 4 * 1024 * 1024
ProjectionName = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
StateType = TypeVar("StateType", bound=StrictProtocolModel)


class ProjectionErrorCode(StrEnum):
    CHECKPOINT_MISMATCH = "checkpoint_mismatch"
    EVENT_ORDER = "event_order"
    REDUCER_FAILURE = "reducer_failure"
    STATE_TYPE = "state_type"
    DIVERGENCE = "divergence"
    PAGE_LIMIT = "page_limit"


class ProjectionError(RuntimeError):
    """Stable replay failure that does not expose event or state contents."""

    def __init__(self, code: ProjectionErrorCode) -> None:
        super().__init__("projection replay failed")
        self.code = code


class ProjectionCheckpoint(StrictProtocolModel):
    workspace_id: WorkspaceId
    projection_name: ProjectionName
    projection_version: SchemaVersion
    last_journal_sequence: int = Field(ge=0, le=2**63 - 1)
    event_count: int = Field(ge=0, le=2**63 - 1)
    state_json: str = Field(min_length=2)
    state_sha256: Sha256

    @model_validator(mode="after")
    def validate_canonical_state(self) -> ProjectionCheckpoint:
        encoded_state = self.state_json.encode()
        if len(encoded_state) > MAXIMUM_PROJECTION_STATE_BYTES:
            raise ValueError("projection state exceeds 4 MiB")
        try:
            parsed_state = json.loads(self.state_json)
        except json.JSONDecodeError as error:
            raise ValueError("projection state must be valid JSON") from error
        if not isinstance(parsed_state, dict):
            raise ValueError("projection state must be a JSON object")
        if canonical_state_json(parsed_state) != self.state_json:
            raise ValueError("projection state must use canonical JSON")
        actual_sha256 = hashlib.sha256(encoded_state).hexdigest()
        if actual_sha256 != self.state_sha256:
            raise ValueError("projection state hash mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ProjectionDefinition[DefinitionState: StrictProtocolModel]:
    name: ProjectionName
    version: SchemaVersion
    state_model: type[DefinitionState]
    initial_state: DefinitionState
    reducer: Callable[[DefinitionState, JournalEvent], DefinitionState]

    def __post_init__(self) -> None:
        TypeAdapter(ProjectionName).validate_python(self.name, strict=True)
        TypeAdapter(SchemaVersion).validate_python(self.version, strict=True)
        if not isinstance(self.initial_state, self.state_model):
            raise TypeError("projection initial state has the wrong model")
        if not callable(self.reducer):
            raise TypeError("projection reducer must be callable")


def canonical_state_json(state: object) -> str:
    try:
        return json.dumps(
            state,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("projection state must be finite JSON") from error
