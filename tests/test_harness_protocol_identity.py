from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services.harness.protocol import (
    AuthenticationMethod,
    DecisionOutcome,
    GrantRecord,
    GrantState,
    PolicyDecisionRecord,
    PrincipalRecord,
    TraceLink,
    WorkspaceRecord,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
DIGEST = "0" * 64
POLICY_VERSION = f"pol_{DIGEST}"


def identifier(prefix: str) -> str:
    return f"{prefix}_0123456789abcdef0123456789abcdef"


def principal() -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=identifier("prn"),
        authentication_method=AuthenticationMethod.OIDC,
        issuer="https://identity.example.test",
        subject_sha256=DIGEST,
        session_binding_sha256="1" * 64,
        authenticated_at=NOW,
    )


def grant(**overrides: object) -> GrantRecord:
    values: dict[str, object] = {
        "grant_id": identifier("grt"),
        "principal_id": identifier("prn"),
        "workspace_id": identifier("wsp"),
        "roles": ("developer", "reviewer"),
        "capabilities": ("artifact.read", "tool.execute"),
        "policy_version": POLICY_VERSION,
        "state": GrantState.ACTIVE,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return GrantRecord.model_validate(values)


def test_principal_is_strict_immutable_and_server_derived() -> None:
    record = principal()

    assert record.authentication_method is AuthenticationMethod.OIDC
    with pytest.raises(ValidationError, match="frozen"):
        record.principal_id = identifier("prn")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PrincipalRecord.model_validate(
            {**record.model_dump(), "untrusted_role": "administrator"}
        )


def test_grant_requires_canonical_authority_and_valid_time_window() -> None:
    assert grant().roles == ("developer", "reviewer")

    with pytest.raises(ValidationError, match="roles must be unique and sorted"):
        grant(roles=("reviewer", "developer"))
    with pytest.raises(ValidationError, match="capabilities must be unique and sorted"):
        grant(capabilities=("tool.execute", "tool.execute"))
    with pytest.raises(ValidationError, match="expiry"):
        grant(expires_at=NOW)


def test_revocation_metadata_must_match_grant_state() -> None:
    revoked_at = NOW + timedelta(minutes=30)

    assert (
        grant(state=GrantState.REVOKED, revoked_at=revoked_at).revoked_at
        == revoked_at
    )
    with pytest.raises(ValidationError, match="requires revoked_at"):
        grant(state=GrantState.REVOKED)
    with pytest.raises(ValidationError, match="only a revoked grant"):
        grant(revoked_at=revoked_at)
    with pytest.raises(ValidationError, match="inside the grant window"):
        grant(
            state=GrantState.REVOKED,
            revoked_at=NOW + timedelta(hours=2),
        )


def workspace(root_uri: str) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=identifier("wsp"),
        tenant_id=identifier("ten"),
        owner_principal_id=identifier("prn"),
        root_uri=root_uri,
        repository_fingerprint_sha256=DIGEST,
        policy_version=POLICY_VERSION,
        created_at=NOW,
    )


def test_workspace_accepts_only_canonical_local_file_roots() -> None:
    assert workspace("file:///srv/workspaces/project%20one").root_uri.endswith(
        "project%20one"
    )
    assert workspace("file://localhost/srv/project").root_uri.startswith("file://")

    invalid_roots = (
        "https://example.test/repository",
        "file://remote.example.test/srv/project",
        "file://user@localhost/srv/project",
        "file://localhost:abc/srv/project",
        "file:///srv/../secret",
        "file:///srv/./project",
        "file:///srv//project",
        "file:///srv/project/",
        "file:///srv/project?revision=main",
        "file:///srv/project%2fone",
    )
    for invalid_root in invalid_roots:
        with pytest.raises(ValidationError):
            workspace(invalid_root)


def test_policy_decision_requires_canonical_rule_evidence() -> None:
    trace = TraceLink(
        request_id=identifier("req"),
        correlation_id=identifier("evt"),
    )
    decision = PolicyDecisionRecord(
        decision_id=identifier("dcs"),
        outcome=DecisionOutcome.ALLOW,
        capability="tool.execute",
        policy_version=POLICY_VERSION,
        matched_rule_ids=("allow-local-tools", "workspace-member"),
        reason="Grant and workspace policy allow this capability.",
        evaluated_at=NOW,
        trace=trace,
    )

    assert decision.trace == trace
    with pytest.raises(ValidationError, match="matched_rule_ids"):
        PolicyDecisionRecord.model_validate(
            {
                **decision.model_dump(),
                "outcome": DecisionOutcome.DENY,
                "matched_rule_ids": ("workspace-member", "allow-local-tools"),
            }
        )
