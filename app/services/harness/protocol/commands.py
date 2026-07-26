"""Versioned command envelope for authenticated harness clients."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ClientId,
    RequestId,
    SchemaVersion,
    StrictProtocolModel,
    WorkspaceId,
)
from app.services.harness.protocol.command_base import QueryCommand
from app.services.harness.protocol.commands_operations import OperationsCommandTypes
from app.services.harness.protocol.commands_session import SessionCommandTypes

Command = Annotated[
    SessionCommandTypes | OperationsCommandTypes,
    Field(discriminator="kind"),
]


class CommandEnvelope(StrictProtocolModel):
    """Client selectors and one typed command; never proof of authority."""

    schema_version: SchemaVersion
    request_id: RequestId
    client_id: ClientId
    workspace_id: WorkspaceId
    expected_sequence: int | None = Field(default=None, ge=0, le=2**63 - 1)
    command: Command

    @model_validator(mode="after")
    def validate_sequence_precondition(self) -> Self:
        if isinstance(self.command, QueryCommand):
            if self.expected_sequence is not None:
                raise ValueError("query command cannot carry expected_sequence")
        return self
