"""Neutral evidence contracts used during deterministic startup recovery."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedReason,
    OperationId,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.execution import (
    OperationRecord,
    ToolName,
    ToolVersion,
)


class OperationRecoveryAction(StrEnum):
    PRESERVE = "preserve"
    RESUME_PREPARED = "resume_prepared"
    RETRY_IDEMPOTENT = "retry_idempotent"
    NEEDS_OPERATOR = "needs_operator"


class LeaseRecoveryState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


class RecoveryLease(StrictProtocolModel):
    lease_sha256: Sha256
    fencing_generation: int = Field(ge=1, le=2**63 - 1)
    expires_at: UtcTimestamp
    state: LeaseRecoveryState = LeaseRecoveryState.ACTIVE
    expired_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if (self.state is LeaseRecoveryState.EXPIRED) != (
            self.expired_at is not None
        ):
            raise ValueError("expired lease requires expiry evidence")
        if self.expired_at is not None and self.expired_at < self.expires_at:
            raise ValueError("lease cannot expire before its deadline")
        return self


class OperationRecoveryDecision(StrictProtocolModel):
    action: OperationRecoveryAction
    operation: OperationRecord


class OperationStatusProofKind(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    FAILED = "failed"


class OperationStatusProof(StrictProtocolModel):
    operation_id: OperationId
    tool_name: ToolName
    tool_version: ToolVersion
    args_sha256: Sha256
    fencing_token: int = Field(ge=1, le=2**63 - 1)
    kind: OperationStatusProofKind
    status_request_sha256: Sha256
    status_response_sha256: Sha256
    result_sha256: Sha256 | None = None
    failure_reason: BoundedReason | None = None
    observed_at: UtcTimestamp
    proof_sha256: Sha256

    @model_validator(mode="after")
    def validate_proof(self) -> Self:
        completed = self.kind is OperationStatusProofKind.COMPLETED
        failed = self.kind is OperationStatusProofKind.FAILED
        if completed != (self.result_sha256 is not None):
            raise ValueError("completed proof requires exactly one result hash")
        if failed != (self.failure_reason is not None):
            raise ValueError("failed proof requires exactly one failure reason")
        expected = operation_status_proof_sha256(
            operation_id=self.operation_id,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            args_sha256=self.args_sha256,
            fencing_token=self.fencing_token,
            kind=self.kind,
            status_request_sha256=self.status_request_sha256,
            status_response_sha256=self.status_response_sha256,
            result_sha256=self.result_sha256,
            failure_reason=self.failure_reason,
            observed_at=self.observed_at,
        )
        if self.proof_sha256 != expected:
            raise ValueError("operation status proof hash is invalid")
        return self


class OperationReconciliationOutcome(StrEnum):
    RETRY_PROVEN_NOT_STARTED = "retry_proven_not_started"
    TERMINAL_PROVEN = "terminal_proven"


class OperationReconciliationDecision(StrictProtocolModel):
    outcome: OperationReconciliationOutcome
    operation: OperationRecord
    proof_sha256: Sha256


def operation_status_proof_sha256(**values: object) -> str:
    encoded = json.dumps(
        values,
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
