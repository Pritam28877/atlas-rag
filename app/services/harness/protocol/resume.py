"""Canonical durable records for command replay and event resume."""

from __future__ import annotations

import hashlib
import json
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    IdempotencyKey,
    Payload,
    PrincipalId,
    Sha256,
    StrictProtocolModel,
    SubscriptionId,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.command_contracts import (
    CommandKind,
    CommandResponseKind,
)
from app.services.harness.protocol.commands import CommandEnvelope

MAXIMUM_JOURNAL_SEQUENCE = 2**63 - 1


def command_request_sha256(envelope: CommandEnvelope) -> Sha256:
    """Hash semantic command fields while ignoring reconnect transport IDs."""

    semantic_request = envelope.model_dump(
        mode="json",
        exclude={"request_id", "client_id"},
    )
    encoded = json.dumps(
        semantic_request,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def command_result_sha256(result: Payload) -> Sha256:
    """Hash the canonical Pydantic JSON representation of a result payload."""

    return hashlib.sha256(result.model_dump_json().encode()).hexdigest()


class CommandReplayReceipt(StrictProtocolModel):
    """One immutable successful command result bound to its semantic request."""

    workspace_id: WorkspaceId
    principal_id: PrincipalId
    idempotency_key: IdempotencyKey
    command_kind: CommandKind
    request_sha256: Sha256
    response_kind: CommandResponseKind
    result: Payload
    result_sha256: Sha256
    committed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_result_digest(self) -> Self:
        if command_result_sha256(self.result) != self.result_sha256:
            raise ValueError("command result hash does not match payload")
        return self


class SubscriptionCursorRecord(StrictProtocolModel):
    """Owned monotonic delivery and acknowledgement position for reconnect."""

    workspace_id: WorkspaceId
    principal_id: PrincipalId
    subscription_id: SubscriptionId
    generation: int = Field(ge=1, le=MAXIMUM_JOURNAL_SEQUENCE)
    acknowledged_sequence: int = Field(
        ge=0,
        le=MAXIMUM_JOURNAL_SEQUENCE,
    )
    delivered_sequence: int = Field(ge=0, le=MAXIMUM_JOURNAL_SEQUENCE)
    updated_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_cursor_window(self) -> Self:
        if self.acknowledged_sequence > self.delivered_sequence:
            raise ValueError("acknowledgement cannot exceed delivered sequence")
        if self.expires_at <= self.updated_at:
            raise ValueError("subscription cursor expiry must follow update")
        return self
