import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.harness.protocol import (
    AuthenticationMethod,
    CommandEnvelope,
    GrantRecord,
    GrantState,
    PrincipalRecord,
    WorkspaceRecord,
)
from app.services.harness.runtime import (
    AuthorityDenialReason,
    AuthorityDeniedError,
    AuthoritySnapshot,
    CommandAuthorityBinder,
    VerifiedPeerSession,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 26, 14, 0, tzinfo=UTC)
POLICY_VERSION = f"pol_{'0' * 64}"
WORKSPACE_ID = "wsp_0123456789abcdef0123456789abcdef"


def identifier(prefix: str, character: str = "0") -> str:
    return f"{prefix}_{character * 32}"


def principal(character: str = "0") -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=identifier("prn", character),
        authentication_method=AuthenticationMethod.PEER_CREDENTIALS,
        issuer="atlas-local-peer",
        subject_sha256=character * 64,
        session_binding_sha256="1" * 64,
        authenticated_at=NOW - timedelta(seconds=1),
    )


def session(
    *,
    principal_record: PrincipalRecord | None = None,
    expires_at: datetime | None = None,
) -> VerifiedPeerSession:
    return VerifiedPeerSession(
        principal=principal_record or principal(),
        token_id_sha256="2" * 64,
        expires_at=expires_at or NOW + timedelta(seconds=30),
    )


def workspace(character: str = "0") -> WorkspaceRecord:
    workspace_id = WORKSPACE_ID
    if character != "0":
        workspace_id = identifier("wsp", character)
    return WorkspaceRecord(
        workspace_id=workspace_id,
        tenant_id=identifier("ten"),
        owner_principal_id=identifier("prn"),
        root_uri=f"file:///srv/workspaces/{character}",
        repository_fingerprint_sha256="3" * 64,
        policy_version=POLICY_VERSION,
        created_at=NOW - timedelta(days=1),
    )


def grant(**overrides: object) -> GrantRecord:
    values: dict[str, object] = {
        "grant_id": identifier("grt"),
        "principal_id": identifier("prn"),
        "workspace_id": WORKSPACE_ID,
        "roles": ("developer",),
        "capabilities": ("workspace.open",),
        "policy_version": POLICY_VERSION,
        "state": GrantState.ACTIVE,
        "issued_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return GrantRecord.model_validate(values)


def envelope() -> CommandEnvelope:
    path = ROOT / "tests/fixtures/harness/protocol/reader-v1.0-command.json"
    return CommandEnvelope.model_validate_json(path.read_bytes())


class FakeAuthorityRepository:
    def __init__(self, snapshot: AuthoritySnapshot | None) -> None:
        self.snapshot = snapshot
        self.requested_workspace_id: str | None = None

    async def load_authority(self, workspace_id: str) -> AuthoritySnapshot | None:
        self.requested_workspace_id = workspace_id
        return self.snapshot


def binder(snapshot: AuthoritySnapshot | None) -> tuple[
    CommandAuthorityBinder,
    FakeAuthorityRepository,
]:
    repository = FakeAuthorityRepository(snapshot)
    return (
        CommandAuthorityBinder(repository, clock=lambda: NOW),
        repository,
    )


def test_command_binds_server_principal_current_grant_and_workspace() -> None:
    snapshot = AuthoritySnapshot(workspace=workspace(), grants=(grant(),))
    authority_binder, repository = binder(snapshot)

    context = asyncio.run(authority_binder.bind(session(), envelope()))

    assert repository.requested_workspace_id == envelope().workspace_id
    assert context.principal == session().principal
    assert context.grant == grant()
    assert context.contract.authorization_capability == "workspace.open"
    assert context.authorized_at == NOW


def test_workspace_selector_never_proves_authority() -> None:
    substituted_workspace = workspace("1")
    snapshot = AuthoritySnapshot(
        workspace=substituted_workspace,
        grants=(
            grant(
                grant_id=identifier("grt", "1"),
                workspace_id=substituted_workspace.workspace_id,
            ),
        ),
    )
    authority_binder, _ = binder(snapshot)

    with pytest.raises(AuthorityDeniedError) as denied:
        asyncio.run(authority_binder.bind(session(), envelope()))

    assert denied.value.reason is AuthorityDenialReason.WORKSPACE_SUBSTITUTION
    assert str(denied.value) == "command authority denied"


@pytest.mark.parametrize(
    ("grant_overrides", "expected_reason"),
    (
        (
            {"principal_id": identifier("prn", "1")},
            AuthorityDenialReason.PRINCIPAL_MISMATCH,
        ),
        (
            {"capabilities": ("thread.list",)},
            AuthorityDenialReason.CAPABILITY_MISSING,
        ),
        (
            {"policy_version": f"pol_{'1' * 64}"},
            AuthorityDenialReason.POLICY_VERSION_MISMATCH,
        ),
        (
            {"expires_at": NOW},
            AuthorityDenialReason.NO_ACTIVE_GRANT,
        ),
        (
            {
                "state": GrantState.REVOKED,
                "revoked_at": NOW - timedelta(minutes=1),
            },
            AuthorityDenialReason.NO_ACTIVE_GRANT,
        ),
    ),
)
def test_each_authority_dimension_fails_closed(
    grant_overrides: dict[str, object],
    expected_reason: AuthorityDenialReason,
) -> None:
    snapshot = AuthoritySnapshot(
        workspace=workspace(),
        grants=(grant(**grant_overrides),),
    )
    authority_binder, _ = binder(snapshot)

    with pytest.raises(AuthorityDeniedError) as denied:
        asyncio.run(authority_binder.bind(session(), envelope()))

    assert denied.value.reason is expected_reason


def test_missing_workspace_and_expired_session_are_denied() -> None:
    missing_binder, _ = binder(None)
    with pytest.raises(AuthorityDeniedError) as missing:
        asyncio.run(missing_binder.bind(session(), envelope()))
    assert missing.value.reason is AuthorityDenialReason.WORKSPACE_NOT_FOUND

    snapshot = AuthoritySnapshot(workspace=workspace(), grants=(grant(),))
    expired_binder, repository = binder(snapshot)
    with pytest.raises(AuthorityDeniedError) as expired:
        asyncio.run(
            expired_binder.bind(
                session(expires_at=NOW),
                envelope(),
            )
        )
    assert expired.value.reason is AuthorityDenialReason.SESSION_EXPIRED
    assert repository.requested_workspace_id is None
