"""Canonical hash-bound approval lifecycle fixtures."""

from datetime import timedelta

from app.services.harness.policy import (
    PolicyEffect,
    PolicyLayer,
    compile_capability_decision,
    request_capability_approval,
    respond_to_approval,
)
from app.services.harness.protocol import ApprovalScope
from app.services.harness.protocol.approvals import DurableApprovalState
from tests.harness.policy.fixtures import (
    DECISION_ID,
    NOW,
    TRACE,
    bundle,
    document,
    proposal,
    rule,
)

APPROVAL_ID = "apr_" + "4" * 32
OPERATION_ID = "opn_" + "5" * 32
WORKSPACE_ID = "wsp_" + "6" * 32
PRINCIPAL_ID = "prn_" + "7" * 32
GRANT_ID = "grt_" + "8" * 32
SCOPE_BINDING = "9" * 64


def asked_decision():
    return compile_capability_decision(
        decision_id=DECISION_ID,
        proposal=proposal(),
        policies=bundle(
            document(
                PolicyLayer.SYSTEM,
                rule("approval-rule", PolicyEffect.ASK),
            )
        ),
        evaluated_at=NOW,
        trace=TRACE,
    )


def pending(scope: ApprovalScope = ApprovalScope.ONCE):
    return request_capability_approval(
        approval_id=APPROVAL_ID,
        operation_id=OPERATION_ID,
        workspace_id=WORKSPACE_ID,
        principal_id=PRINCIPAL_ID,
        grant_id=GRANT_ID,
        decision=asked_decision(),
        proposal=proposal(),
        scope=scope,
        scope_binding_sha256=SCOPE_BINDING,
        requested_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        reason="Exact filesystem write requires approval.",
    )


def approved(scope: ApprovalScope = ApprovalScope.ONCE):
    record, _ = pending(scope)
    return respond_to_approval(
        record,
        authorization_sha256=record.binding.authorization_sha256,
        decision=DurableApprovalState.APPROVED,
        principal_id=PRINCIPAL_ID,
        grant_id=GRANT_ID,
        policy_version=record.binding.policy_version,
        decided_at=NOW + timedelta(minutes=1),
        reason="Approved exact request.",
    )
