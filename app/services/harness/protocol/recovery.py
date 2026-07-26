"""Neutral evidence contracts used during deterministic startup recovery."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.execution import OperationRecord


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
