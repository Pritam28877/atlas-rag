"""Server-owned command authority binding for authenticated local sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Never, Protocol, Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    CommandContract,
    CommandEnvelope,
    CommandKind,
    GrantRecord,
    GrantState,
    PrincipalRecord,
    StrictProtocolModel,
    UtcTimestamp,
    WorkspaceId,
    WorkspaceRecord,
    command_contract,
)
from app.services.harness.runtime.peer_auth import VerifiedPeerSession


class AuthorityDenialReason(StrEnum):
    SESSION_EXPIRED = "session_expired"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_SUBSTITUTION = "workspace_substitution"
    PRINCIPAL_MISMATCH = "principal_mismatch"
    NO_ACTIVE_GRANT = "no_active_grant"
    CAPABILITY_MISSING = "capability_missing"
    POLICY_VERSION_MISMATCH = "policy_version_mismatch"


class AuthorityDeniedError(PermissionError):
    """Generic client denial with a structured server-side audit reason."""

    def __init__(self, reason: AuthorityDenialReason) -> None:
        super().__init__("command authority denied")
        self.reason = reason


class AuthoritySnapshot(StrictProtocolModel):
    """Bounded current workspace and grants loaded from authoritative storage."""

    workspace: WorkspaceRecord
    grants: tuple[GrantRecord, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_grants(self) -> Self:
        grant_ids = tuple(grant.grant_id for grant in self.grants)
        if tuple(sorted(set(grant_ids))) != grant_ids:
            raise ValueError("authority grants must be unique and sorted")
        if any(
            grant.workspace_id != self.workspace.workspace_id
            for grant in self.grants
        ):
            raise ValueError("authority grant belongs to another workspace")
        return self


class AuthorityRepository(Protocol):
    """Storage boundary that must return a transactionally current snapshot."""

    async def load_authority(
        self,
        workspace_id: WorkspaceId,
    ) -> AuthoritySnapshot | None: ...


class AuthenticatedCommandContext(StrictProtocolModel):
    """Server-derived authority attached after selector validation."""

    principal: PrincipalRecord
    grant: GrantRecord
    workspace: WorkspaceRecord
    contract: CommandContract
    envelope: CommandEnvelope
    authorized_at: UtcTimestamp


class CommandAuthorityBinder:
    """Re-authorizes every command against current workspace and grant state."""

    def __init__(
        self,
        repository: AuthorityRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def bind(
        self,
        session: VerifiedPeerSession,
        envelope: CommandEnvelope,
    ) -> AuthenticatedCommandContext:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("authority clock must return UTC")
        if now >= session.expires_at:
            self._deny(AuthorityDenialReason.SESSION_EXPIRED)
        snapshot = await self._repository.load_authority(envelope.workspace_id)
        if snapshot is None:
            self._deny(AuthorityDenialReason.WORKSPACE_NOT_FOUND)
        if snapshot.workspace.workspace_id != envelope.workspace_id:
            self._deny(AuthorityDenialReason.WORKSPACE_SUBSTITUTION)

        contract = command_contract(CommandKind(envelope.command.kind))
        eligible_grants = [
            grant
            for grant in snapshot.grants
            if self._grant_is_current(
                grant,
                snapshot.workspace,
                now,
            )
        ]
        if not eligible_grants:
            self._deny(AuthorityDenialReason.NO_ACTIVE_GRANT)
        principal_grants = [
            grant
            for grant in eligible_grants
            if grant.principal_id == session.principal.principal_id
        ]
        if not principal_grants:
            self._deny(AuthorityDenialReason.PRINCIPAL_MISMATCH)
        policy_grants = [
            grant
            for grant in principal_grants
            if grant.policy_version == snapshot.workspace.policy_version
        ]
        if not policy_grants:
            self._deny(AuthorityDenialReason.POLICY_VERSION_MISMATCH)
        capability_grants = [
            grant
            for grant in policy_grants
            if contract.authorization_capability in grant.capabilities
        ]
        if not capability_grants:
            self._deny(AuthorityDenialReason.CAPABILITY_MISSING)
        selected_grant = capability_grants[0]
        return AuthenticatedCommandContext(
            principal=session.principal,
            grant=selected_grant,
            workspace=snapshot.workspace,
            contract=contract,
            envelope=envelope,
            authorized_at=now,
        )

    @staticmethod
    def _grant_is_current(
        grant: GrantRecord,
        workspace: WorkspaceRecord,
        now: datetime,
    ) -> bool:
        return (
            grant.state is GrantState.ACTIVE
            and grant.workspace_id == workspace.workspace_id
            and grant.issued_at <= now < grant.expires_at
        )

    @staticmethod
    def _deny(reason: AuthorityDenialReason) -> Never:
        raise AuthorityDeniedError(reason)
