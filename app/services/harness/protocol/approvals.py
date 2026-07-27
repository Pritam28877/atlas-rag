"""Hash-bound durable approval lifecycle records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    ApprovalId,
    BoundedReason,
    Capability,
    DecisionId,
    GrantId,
    OperationId,
    PolicyVersion,
    PrincipalId,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.execution import ApprovalScope


class DurableApprovalState(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CANCELLED = "cancelled"
    CONSUMED = "consumed"


class ApprovalRequestBinding(StrictProtocolModel):
    approval_id: ApprovalId
    operation_id: OperationId
    workspace_id: WorkspaceId
    principal_id: PrincipalId
    grant_id: GrantId
    policy_decision_id: DecisionId
    policy_version: PolicyVersion
    capability: Capability
    target_sha256: Sha256
    proposal_sha256: Sha256
    scope: ApprovalScope
    scope_binding_sha256: Sha256
    requested_at: UtcTimestamp
    expires_at: UtcTimestamp
    authorization_sha256: Sha256

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.expires_at <= self.requested_at:
            raise ValueError("approval expiry must follow request time")
        expected_sha256 = approval_authorization_sha256(
            approval_id=self.approval_id,
            operation_id=self.operation_id,
            workspace_id=self.workspace_id,
            principal_id=self.principal_id,
            grant_id=self.grant_id,
            policy_decision_id=self.policy_decision_id,
            policy_version=self.policy_version,
            capability=self.capability,
            target_sha256=self.target_sha256,
            proposal_sha256=self.proposal_sha256,
            scope=self.scope,
            scope_binding_sha256=self.scope_binding_sha256,
            requested_at=self.requested_at,
            expires_at=self.expires_at,
        )
        if self.authorization_sha256 != expected_sha256:
            raise ValueError("approval authorization hash does not match binding")
        return self


class DurableApprovalRecord(StrictProtocolModel):
    binding: ApprovalRequestBinding
    generation: int = Field(ge=1, le=16)
    state: DurableApprovalState
    reason: BoundedReason
    decided_at: UtcTimestamp | None = None
    decided_by_principal_id: PrincipalId | None = None
    revoked_at: UtcTimestamp | None = None
    revoked_by_principal_id: PrincipalId | None = None
    latest_receipt_sha256: Sha256

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        requested = self.state is DurableApprovalState.REQUESTED
        if requested != (self.generation == 1):
            raise ValueError("only a new approval may remain requested")
        decision_states = {
            DurableApprovalState.APPROVED,
            DurableApprovalState.DENIED,
            DurableApprovalState.REVOKED,
            DurableApprovalState.CONSUMED,
        }
        has_decision = self.decided_at is not None
        if self.state in decision_states and not has_decision:
            raise ValueError("approval decision evidence is inconsistent")
        if self.state in {
            DurableApprovalState.REQUESTED,
            DurableApprovalState.CANCELLED,
        } and has_decision:
            raise ValueError("approval decision evidence is inconsistent")
        if has_decision != (self.decided_by_principal_id is not None):
            raise ValueError("approval deciding actor evidence is inconsistent")
        revoked = self.state is DurableApprovalState.REVOKED
        if revoked != (self.revoked_at is not None):
            raise ValueError("approval revocation evidence is inconsistent")
        if revoked != (self.revoked_by_principal_id is not None):
            raise ValueError("approval revoking actor evidence is inconsistent")
        timestamps = tuple(
            timestamp
            for timestamp in (self.decided_at, self.revoked_at)
            if timestamp is not None
        )
        if any(
            not self.binding.requested_at <= timestamp <= self.binding.expires_at
            for timestamp in timestamps
        ):
            raise ValueError("approval transition falls outside its validity window")
        return self


class ApprovalReceipt(StrictProtocolModel):
    approval_id: ApprovalId
    workspace_id: WorkspaceId
    generation: int = Field(ge=1, le=16)
    state: DurableApprovalState
    authorization_sha256: Sha256
    previous_receipt_sha256: Sha256 | None = None
    transitioned_at: UtcTimestamp
    receipt_sha256: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if (self.generation == 1) != (self.previous_receipt_sha256 is None):
            raise ValueError("approval receipt chain is inconsistent")
        expected_sha256 = approval_receipt_sha256(
            approval_id=self.approval_id,
            workspace_id=self.workspace_id,
            generation=self.generation,
            state=self.state,
            authorization_sha256=self.authorization_sha256,
            previous_receipt_sha256=self.previous_receipt_sha256,
            transitioned_at=self.transitioned_at,
        )
        if self.receipt_sha256 != expected_sha256:
            raise ValueError("approval receipt hash does not match receipt")
        return self


def approval_authorization_sha256(**binding: object) -> str:
    return _canonical_sha256(binding)


def approval_receipt_sha256(**receipt: object) -> str:
    return _canonical_sha256(receipt)


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
