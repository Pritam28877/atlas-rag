"""Fail-closed creation, response, expiry, revocation, and use of approvals."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from app.services.harness.policy.models import (
    CapabilityProposal,
    EffectivePolicyDecision,
    PolicyEffect,
)
from app.services.harness.protocol import (
    ApprovalId,
    ApprovalScope,
    GrantId,
    OperationId,
    PolicyVersion,
    PrincipalId,
    Sha256,
    WorkspaceId,
)
from app.services.harness.protocol.approvals import (
    ApprovalReceipt,
    ApprovalRequestBinding,
    DurableApprovalRecord,
    DurableApprovalState,
    approval_authorization_sha256,
    approval_receipt_sha256,
)

MAXIMUM_APPROVAL_LIFETIME = {
    ApprovalScope.ONCE: timedelta(minutes=15),
    ApprovalScope.SESSION: timedelta(hours=24),
    ApprovalScope.WORKSPACE: timedelta(days=30),
}


class ApprovalAuthorizationError(ValueError):
    """Stable fail-closed rejection without secret-bearing request detail."""


def request_capability_approval(
    *,
    approval_id: ApprovalId,
    operation_id: OperationId,
    workspace_id: WorkspaceId,
    principal_id: PrincipalId,
    grant_id: GrantId,
    decision: EffectivePolicyDecision,
    proposal: CapabilityProposal,
    scope: ApprovalScope,
    scope_binding_sha256: Sha256,
    requested_at: datetime,
    expires_at: datetime,
    reason: str,
) -> tuple[DurableApprovalRecord, ApprovalReceipt]:
    if decision.effect is not PolicyEffect.ASK:
        raise ApprovalAuthorizationError("policy decision does not require approval")
    if (
        decision.request_sha256 != proposal.target_sha256
        or decision.capability != proposal.capability
        or decision.capability_kind is not proposal.target.kind
    ):
        raise ApprovalAuthorizationError("policy decision and proposal do not match")
    if expires_at - requested_at > MAXIMUM_APPROVAL_LIFETIME[scope]:
        raise ApprovalAuthorizationError("approval scope lifetime exceeds its bound")
    proposal_sha256 = _proposal_sha256(proposal)
    authorization_sha256 = approval_authorization_sha256(
        approval_id=approval_id,
        operation_id=operation_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
        grant_id=grant_id,
        policy_decision_id=decision.decision_id,
        policy_version=decision.policy_bundle_version,
        capability=proposal.capability,
        target_sha256=proposal.target_sha256,
        proposal_sha256=proposal_sha256,
        scope=scope,
        scope_binding_sha256=scope_binding_sha256,
        requested_at=requested_at,
        expires_at=expires_at,
    )
    binding = ApprovalRequestBinding(
        approval_id=approval_id,
        operation_id=operation_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
        grant_id=grant_id,
        policy_decision_id=decision.decision_id,
        policy_version=decision.policy_bundle_version,
        capability=proposal.capability,
        target_sha256=proposal.target_sha256,
        proposal_sha256=proposal_sha256,
        scope=scope,
        scope_binding_sha256=scope_binding_sha256,
        requested_at=requested_at,
        expires_at=expires_at,
        authorization_sha256=authorization_sha256,
    )
    receipt = _build_receipt(
        binding=binding,
        generation=1,
        state=DurableApprovalState.REQUESTED,
        previous_receipt_sha256=None,
        transitioned_at=requested_at,
    )
    return (
        DurableApprovalRecord(
            binding=binding,
            generation=1,
            state=DurableApprovalState.REQUESTED,
            reason=reason,
            latest_receipt_sha256=receipt.receipt_sha256,
        ),
        receipt,
    )


def respond_to_approval(
    record: DurableApprovalRecord,
    *,
    authorization_sha256: Sha256,
    decision: DurableApprovalState,
    principal_id: PrincipalId,
    grant_id: GrantId,
    policy_version: PolicyVersion,
    decided_at: datetime,
    reason: str,
) -> tuple[DurableApprovalRecord, ApprovalReceipt]:
    if decision not in {
        DurableApprovalState.APPROVED,
        DurableApprovalState.DENIED,
    }:
        raise ApprovalAuthorizationError("approval response is invalid")
    _require_pending_and_current(
        record,
        authorization_sha256=authorization_sha256,
        principal_id=principal_id,
        grant_id=grant_id,
        policy_version=policy_version,
        observed_at=decided_at,
    )
    updated = record.model_copy(
        update={
            "generation": record.generation + 1,
            "state": decision,
            "reason": reason,
            "decided_at": decided_at,
            "decided_by_principal_id": principal_id,
        }
    )
    return _with_receipt(record, updated, decided_at)


def authorize_with_approval(
    record: DurableApprovalRecord,
    *,
    proposal: CapabilityProposal,
    scope_binding_sha256: Sha256,
    principal_id: PrincipalId,
    grant_id: GrantId,
    policy_version: PolicyVersion,
    observed_at: datetime,
) -> tuple[DurableApprovalRecord, ApprovalReceipt | None]:
    binding = record.binding
    if record.state is not DurableApprovalState.APPROVED:
        raise ApprovalAuthorizationError("approval is not active")
    if not binding.requested_at <= observed_at < binding.expires_at:
        raise ApprovalAuthorizationError("approval is expired")
    if (
        principal_id != binding.principal_id
        or grant_id != binding.grant_id
        or policy_version != binding.policy_version
        or scope_binding_sha256 != binding.scope_binding_sha256
        or proposal.capability != binding.capability
        or proposal.target_sha256 != binding.target_sha256
        or _proposal_sha256(proposal) != binding.proposal_sha256
    ):
        raise ApprovalAuthorizationError("approval binding changed")
    if binding.scope is not ApprovalScope.ONCE:
        return record, None
    consumed = record.model_copy(
        update={
            "generation": record.generation + 1,
            "state": DurableApprovalState.CONSUMED,
        }
    )
    return _with_receipt(record, consumed, observed_at)


def expire_approval(
    record: DurableApprovalRecord,
    *,
    observed_at: datetime,
) -> tuple[DurableApprovalRecord, ApprovalReceipt]:
    if observed_at < record.binding.expires_at:
        raise ApprovalAuthorizationError("approval has not expired")
    if record.state not in {
        DurableApprovalState.REQUESTED,
        DurableApprovalState.APPROVED,
    }:
        raise ApprovalAuthorizationError("approval is already terminal")
    expired = record.model_copy(
        update={
            "generation": record.generation + 1,
            "state": DurableApprovalState.EXPIRED,
        }
    )
    return _with_receipt(record, expired, observed_at)


def revoke_approval(
    record: DurableApprovalRecord,
    *,
    principal_id: PrincipalId,
    grant_id: GrantId,
    policy_version: PolicyVersion,
    revoked_at: datetime,
    reason: str,
) -> tuple[DurableApprovalRecord, ApprovalReceipt]:
    if record.state is not DurableApprovalState.APPROVED:
        raise ApprovalAuthorizationError("only an active approval can be revoked")
    binding = record.binding
    if (
        principal_id != binding.principal_id
        or grant_id != binding.grant_id
        or policy_version != binding.policy_version
        or not binding.requested_at <= revoked_at < binding.expires_at
    ):
        raise ApprovalAuthorizationError("approval revocation is unauthorized")
    revoked = record.model_copy(
        update={
            "generation": record.generation + 1,
            "state": DurableApprovalState.REVOKED,
            "reason": reason,
            "revoked_at": revoked_at,
            "revoked_by_principal_id": principal_id,
        }
    )
    return _with_receipt(record, revoked, revoked_at)


def _require_pending_and_current(
    record: DurableApprovalRecord,
    *,
    authorization_sha256: Sha256,
    principal_id: PrincipalId,
    grant_id: GrantId,
    policy_version: PolicyVersion,
    observed_at: datetime,
) -> None:
    binding = record.binding
    if record.state is not DurableApprovalState.REQUESTED:
        raise ApprovalAuthorizationError("approval is not pending")
    if not binding.requested_at <= observed_at < binding.expires_at:
        raise ApprovalAuthorizationError("approval is expired")
    if (
        authorization_sha256 != binding.authorization_sha256
        or principal_id != binding.principal_id
        or grant_id != binding.grant_id
        or policy_version != binding.policy_version
    ):
        raise ApprovalAuthorizationError("approval response binding changed")


def _with_receipt(
    previous: DurableApprovalRecord,
    updated: DurableApprovalRecord,
    transitioned_at: datetime,
) -> tuple[DurableApprovalRecord, ApprovalReceipt]:
    receipt = _build_receipt(
        binding=updated.binding,
        generation=updated.generation,
        state=updated.state,
        previous_receipt_sha256=previous.latest_receipt_sha256,
        transitioned_at=transitioned_at,
    )
    validated = DurableApprovalRecord.model_validate(
        {
            **updated.model_dump(),
            "latest_receipt_sha256": receipt.receipt_sha256,
        }
    )
    return validated, receipt


def _build_receipt(
    *,
    binding: ApprovalRequestBinding,
    generation: int,
    state: DurableApprovalState,
    previous_receipt_sha256: Sha256 | None,
    transitioned_at: datetime,
) -> ApprovalReceipt:
    receipt_sha256 = approval_receipt_sha256(
        approval_id=binding.approval_id,
        workspace_id=binding.workspace_id,
        generation=generation,
        state=state,
        authorization_sha256=binding.authorization_sha256,
        previous_receipt_sha256=previous_receipt_sha256,
        transitioned_at=transitioned_at,
    )
    return ApprovalReceipt(
        approval_id=binding.approval_id,
        workspace_id=binding.workspace_id,
        generation=generation,
        state=state,
        authorization_sha256=binding.authorization_sha256,
        previous_receipt_sha256=previous_receipt_sha256,
        transitioned_at=transitioned_at,
        receipt_sha256=receipt_sha256,
    )


def _proposal_sha256(proposal: CapabilityProposal) -> str:
    encoded = json.dumps(
        proposal.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
