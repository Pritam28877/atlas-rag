"""Exact approval binding and bounded lifecycle tests."""

from datetime import timedelta

import pytest

from app.services.harness.policy import (
    ApprovalAuthorizationError,
    CapabilityLimits,
    authorize_with_approval,
    expire_approval,
    request_capability_approval,
    respond_to_approval,
    revoke_approval,
)
from app.services.harness.protocol import ApprovalScope
from app.services.harness.protocol.approvals import DurableApprovalState
from tests.harness.policy.approval_fixtures import (
    APPROVAL_ID,
    GRANT_ID,
    OPERATION_ID,
    PRINCIPAL_ID,
    SCOPE_BINDING,
    WORKSPACE_ID,
    approved,
    asked_decision,
    pending,
)
from tests.harness.policy.fixtures import (
    NOW,
    proposal,
)


def test_request_and_response_form_an_immutable_receipt_chain() -> None:
    requested, request_receipt = pending()
    approved_record, approval_receipt = respond_to_approval(
        requested,
        authorization_sha256=requested.binding.authorization_sha256,
        decision=DurableApprovalState.APPROVED,
        principal_id=PRINCIPAL_ID,
        grant_id=GRANT_ID,
        policy_version=requested.binding.policy_version,
        decided_at=NOW + timedelta(minutes=1),
        reason="Approved exact request.",
    )

    assert requested.state is DurableApprovalState.REQUESTED
    assert approved_record.state is DurableApprovalState.APPROVED
    assert approval_receipt.previous_receipt_sha256 == (
        request_receipt.receipt_sha256
    )
    assert approved_record.latest_receipt_sha256 == approval_receipt.receipt_sha256


@pytest.mark.parametrize(
    ("field_name", "mutated_value"),
    (
        ("principal_id", "prn_" + "a" * 32),
        ("grant_id", "grt_" + "b" * 32),
        ("policy_version", "pol_" + "c" * 64),
    ),
)
def test_mutated_actor_grant_or_policy_cannot_approve(
    field_name: str,
    mutated_value: str,
) -> None:
    record, _ = pending()
    response_values = {
        "authorization_sha256": record.binding.authorization_sha256,
        "decision": DurableApprovalState.APPROVED,
        "principal_id": PRINCIPAL_ID,
        "grant_id": GRANT_ID,
        "policy_version": record.binding.policy_version,
        "decided_at": NOW + timedelta(minutes=1),
        "reason": "Attempt approval.",
        field_name: mutated_value,
    }

    with pytest.raises(ApprovalAuthorizationError, match="binding changed"):
        respond_to_approval(record, **response_values)


def test_mutated_proposal_or_scope_cannot_reuse_approval() -> None:
    record, _ = approved(ApprovalScope.SESSION)
    broader_proposal = proposal().model_copy(
        update={
            "sandbox_limits": CapabilityLimits(
                max_duration_ms=20_000,
                max_cpu_ms=5_000,
                max_memory_bytes=512 * 1024 * 1024,
                max_output_bytes=1_000_000,
                max_processes=8,
            )
        }
    )

    with pytest.raises(ApprovalAuthorizationError, match="binding changed"):
        authorize_with_approval(
            record,
            proposal=broader_proposal,
            scope_binding_sha256=SCOPE_BINDING,
            principal_id=PRINCIPAL_ID,
            grant_id=GRANT_ID,
            policy_version=record.binding.policy_version,
            observed_at=NOW + timedelta(minutes=2),
        )
    with pytest.raises(ApprovalAuthorizationError, match="binding changed"):
        authorize_with_approval(
            record,
            proposal=proposal(),
            scope_binding_sha256="d" * 64,
            principal_id=PRINCIPAL_ID,
            grant_id=GRANT_ID,
            policy_version=record.binding.policy_version,
            observed_at=NOW + timedelta(minutes=2),
        )


def test_once_approval_is_consumed_and_cannot_be_reused() -> None:
    record, _ = approved()
    consumed, receipt = authorize_with_approval(
        record,
        proposal=proposal(),
        scope_binding_sha256=SCOPE_BINDING,
        principal_id=PRINCIPAL_ID,
        grant_id=GRANT_ID,
        policy_version=record.binding.policy_version,
        observed_at=NOW + timedelta(minutes=2),
    )

    assert consumed.state is DurableApprovalState.CONSUMED
    assert receipt is not None
    with pytest.raises(ApprovalAuthorizationError, match="not active"):
        authorize_with_approval(
            consumed,
            proposal=proposal(),
            scope_binding_sha256=SCOPE_BINDING,
            principal_id=PRINCIPAL_ID,
            grant_id=GRANT_ID,
            policy_version=record.binding.policy_version,
            observed_at=NOW + timedelta(minutes=3),
        )


def test_session_approval_can_be_revoked_and_expired() -> None:
    record, _ = approved(ApprovalScope.SESSION)
    revoked, _ = revoke_approval(
        record,
        principal_id=PRINCIPAL_ID,
        grant_id=GRANT_ID,
        policy_version=record.binding.policy_version,
        revoked_at=NOW + timedelta(minutes=2),
        reason="Access no longer needed.",
    )
    assert revoked.state is DurableApprovalState.REVOKED

    pending_record, _ = pending(ApprovalScope.SESSION)
    expired, _ = expire_approval(
        pending_record,
        observed_at=pending_record.binding.expires_at,
    )
    assert expired.state is DurableApprovalState.EXPIRED


def test_scope_lifetimes_are_bounded() -> None:
    with pytest.raises(ApprovalAuthorizationError, match="lifetime"):
        request_capability_approval(
            approval_id=APPROVAL_ID,
            operation_id=OPERATION_ID,
            workspace_id=WORKSPACE_ID,
            principal_id=PRINCIPAL_ID,
            grant_id=GRANT_ID,
            decision=asked_decision(),
            proposal=proposal(),
            scope=ApprovalScope.ONCE,
            scope_binding_sha256=SCOPE_BINDING,
            requested_at=NOW,
            expires_at=NOW + timedelta(minutes=16),
            reason="Too broad.",
        )
