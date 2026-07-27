"""Canonical operation requests, fencing, and synchronous durability receipts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ApprovalId,
    Capability,
    DecisionId,
    IdempotencyKey,
    OperationId,
    Sha256,
    StrictProtocolModel,
    TurnId,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.execution import (
    IdempotencyClass,
    OperationLimits,
    OperationRecord,
    ToolName,
    ToolVersion,
)
from app.services.harness.protocol.states import OperationState

MAXIMUM_ARGUMENT_BYTES = 64 * 1024
MAXIMUM_ARGUMENT_DEPTH = 16
MAXIMUM_ARGUMENT_NODES = 4_096


class OperationDurability(StrEnum):
    SYNCHRONOUS = "synchronous"


class CanonicalOperationRequest(StrictProtocolModel):
    workspace_id: WorkspaceId
    turn_id: TurnId
    idempotency_key: IdempotencyKey
    idempotency_class: IdempotencyClass
    attempt: int = Field(ge=1, le=16)
    tool_name: ToolName
    tool_version: ToolVersion
    args_sha256: Sha256
    capability: Capability
    policy_decision_id: DecisionId
    approval_id: ApprovalId | None = None
    limits: OperationLimits
    operation_id: OperationId

    @model_validator(mode="after")
    def validate_operation_id(self) -> Self:
        expected = stable_operation_id(
            workspace_id=self.workspace_id,
            turn_id=self.turn_id,
            idempotency_key=self.idempotency_key,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            args_sha256=self.args_sha256,
            attempt=self.attempt,
        )
        if self.operation_id != expected:
            raise ValueError("operation ID does not match canonical request")
        return self


class OperationFence(StrictProtocolModel):
    workspace_id: WorkspaceId
    operation_id: OperationId
    lease_sha256: Sha256
    fencing_token: int = Field(ge=1, le=2**63 - 1)
    issued_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("operation fence expiry must follow issuance")
        return self


class OperationDurabilityReceipt(StrictProtocolModel):
    workspace_id: WorkspaceId
    operation_id: OperationId
    state: OperationState
    fencing_token: int = Field(ge=1, le=2**63 - 1)
    operation_sha256: Sha256
    previous_receipt_sha256: Sha256 | None = None
    durability: OperationDurability = OperationDurability.SYNCHRONOUS
    durable_at: UtcTimestamp
    receipt_sha256: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if self.state not in {
            OperationState.PREPARED,
            OperationState.DISPATCHED,
        }:
            raise ValueError("durability receipt must admit prepare or dispatch")
        if (self.state is OperationState.PREPARED) != (
            self.previous_receipt_sha256 is None
        ):
            raise ValueError("operation durability receipt chain is invalid")
        expected = operation_receipt_sha256(
            workspace_id=self.workspace_id,
            operation_id=self.operation_id,
            state=self.state,
            fencing_token=self.fencing_token,
            operation_sha256=self.operation_sha256,
            previous_receipt_sha256=self.previous_receipt_sha256,
            durability=self.durability,
            durable_at=self.durable_at,
        )
        if self.receipt_sha256 != expected:
            raise ValueError("operation durability receipt hash is invalid")
        return self


class PreparedOperation(StrictProtocolModel):
    operation: OperationRecord
    fence_sha256: Sha256
    receipt: OperationDurabilityReceipt

    @model_validator(mode="after")
    def validate_prepared(self) -> Self:
        if (
            self.operation.state is not OperationState.PREPARED
            or self.receipt.state is not OperationState.PREPARED
            or self.receipt.operation_id != self.operation.operation_id
            or self.receipt.operation_sha256 != operation_record_sha256(
                self.operation
            )
        ):
            raise ValueError("prepared operation evidence is inconsistent")
        return self


class OperationDispatchPermit(StrictProtocolModel):
    operation: OperationRecord
    fence_sha256: Sha256
    prepared_receipt: OperationDurabilityReceipt
    dispatched_receipt: OperationDurabilityReceipt

    @model_validator(mode="after")
    def validate_dispatched(self) -> Self:
        if (
            self.operation.state is not OperationState.DISPATCHED
            or self.dispatched_receipt.state is not OperationState.DISPATCHED
            or self.dispatched_receipt.operation_id != self.operation.operation_id
            or self.dispatched_receipt.operation_sha256
            != operation_record_sha256(self.operation)
            or self.dispatched_receipt.previous_receipt_sha256
            != self.prepared_receipt.receipt_sha256
        ):
            raise ValueError("dispatch permit evidence is inconsistent")
        return self


def canonical_operation_args_sha256(arguments: object) -> str:
    node_count = [0]
    _validate_argument_value(arguments, depth=0, node_count=node_count)
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    if len(encoded) > MAXIMUM_ARGUMENT_BYTES:
        raise ValueError("operation arguments exceed 64 KiB")
    return hashlib.sha256(encoded).hexdigest()


def stable_operation_id(
    *,
    workspace_id: str,
    turn_id: str,
    idempotency_key: str,
    tool_name: str,
    tool_version: str,
    args_sha256: str,
    attempt: int,
) -> str:
    values = {
        "workspace_id": workspace_id,
        "turn_id": turn_id,
        "idempotency_key": idempotency_key,
        "tool_name": tool_name,
        "tool_version": tool_version,
        "args_sha256": args_sha256,
        "attempt": attempt,
    }
    digest = _canonical_sha256(values)
    return f"opn_{digest[:32]}"


def operation_record_sha256(operation: OperationRecord) -> str:
    return _canonical_sha256(operation.model_dump(mode="json"))


def operation_fence_sha256(fence: OperationFence) -> str:
    return _canonical_sha256(fence.model_dump(mode="json"))


def operation_receipt_sha256(**values: object) -> str:
    return _canonical_sha256(values)


def _validate_argument_value(
    value: object,
    *,
    depth: int,
    node_count: list[int],
) -> None:
    node_count[0] += 1
    if node_count[0] > MAXIMUM_ARGUMENT_NODES:
        raise ValueError("operation arguments exceed node limit")
    if depth > MAXIMUM_ARGUMENT_DEPTH:
        raise ValueError("operation arguments exceed nesting limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        if len(value) > 32_768 or "\x00" in value:
            raise ValueError("operation argument string is invalid")
        return
    if isinstance(value, list):
        if len(value) > 256:
            raise ValueError("operation argument list exceeds item limit")
        for item in value:
            _validate_argument_value(
                item,
                depth=depth + 1,
                node_count=node_count,
            )
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("operation argument object exceeds field limit")
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 128
                or "\x00" in key
            ):
                raise ValueError("operation argument key is invalid")
            _validate_argument_value(
                item,
                depth=depth + 1,
                node_count=node_count,
            )
        return
    raise ValueError("operation arguments contain a non-canonical value")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"value is not canonically serializable: {type(value).__name__}")
