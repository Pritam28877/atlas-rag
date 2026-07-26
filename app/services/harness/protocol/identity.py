"""Server-derived identity, authorization grant, and workspace records."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Self
from urllib.parse import quote, unquote, urlsplit

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    BoundedReason,
    Capability,
    DecisionId,
    GrantId,
    PolicyVersion,
    PrincipalId,
    Sha256,
    StrictProtocolModel,
    TenantId,
    TraceLink,
    UtcTimestamp,
    WorkspaceId,
)
from app.services.harness.protocol.states import DecisionOutcome, GrantState

type Issuer = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[^\x00-\x20\x7f]+$",
    ),
]
type Role = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$",
    ),
]
type RootUri = Annotated[str, StringConstraints(min_length=8, max_length=4096)]


class AuthenticationMethod(StrEnum):
    PEER_CREDENTIALS = "peer_credentials"
    OIDC = "oidc"
    SESSION_TOKEN = "session_token"
    SERVICE_IDENTITY = "service_identity"


class PrincipalRecord(StrictProtocolModel):
    """Authenticated actor derived by the server, never accepted from a command."""

    principal_id: PrincipalId
    authentication_method: AuthenticationMethod
    issuer: Issuer
    subject_sha256: Sha256
    session_binding_sha256: Sha256
    authenticated_at: UtcTimestamp


def _require_canonical_values(values: tuple[str, ...], field_name: str) -> None:
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{field_name} must be unique and sorted")


class GrantRecord(StrictProtocolModel):
    """Time-bounded workspace authority bound to one authenticated principal."""

    grant_id: GrantId
    principal_id: PrincipalId
    workspace_id: WorkspaceId
    roles: tuple[Role, ...] = Field(min_length=1, max_length=16)
    capabilities: tuple[Capability, ...] = Field(min_length=1, max_length=64)
    policy_version: PolicyVersion
    state: GrantState
    issued_at: UtcTimestamp
    expires_at: UtcTimestamp
    revoked_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_authority_window(self) -> Self:
        _require_canonical_values(self.roles, "roles")
        _require_canonical_values(self.capabilities, "capabilities")
        if self.expires_at <= self.issued_at:
            raise ValueError("grant expiry must follow issuance")
        if self.state is GrantState.REVOKED:
            if self.revoked_at is None:
                raise ValueError("revoked grant requires revoked_at")
            if not self.issued_at <= self.revoked_at <= self.expires_at:
                raise ValueError("revoked_at must be inside the grant window")
        elif self.revoked_at is not None:
            raise ValueError("only a revoked grant may contain revoked_at")
        return self


def _require_canonical_root_uri(root_uri: str) -> None:
    parsed = urlsplit(root_uri)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        raise ValueError("workspace root must be a query-free file URI")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("workspace root contains an invalid port") from error
    if parsed.username or parsed.password or port:
        raise ValueError("workspace root cannot contain authority credentials or port")
    if parsed.hostname not in {None, "", "localhost"}:
        raise ValueError("workspace root cannot name a remote host")
    decoded_path = unquote(parsed.path)
    path = PurePosixPath(decoded_path)
    raw_path_parts = decoded_path.split("/")
    if (
        not decoded_path.startswith("/")
        or any(part in {".", ".."} for part in raw_path_parts)
        or not path.is_absolute()
        or path.as_posix() != decoded_path
    ):
        raise ValueError("workspace root must be an absolute traversal-free path")
    canonical_path = quote(decoded_path, safe="/:@-._~")
    if canonical_path != parsed.path:
        raise ValueError("workspace root URI must use canonical percent encoding")


class WorkspaceRecord(StrictProtocolModel):
    """Authorized canonical workspace selected independently of model output."""

    workspace_id: WorkspaceId
    tenant_id: TenantId
    owner_principal_id: PrincipalId
    root_uri: RootUri
    repository_fingerprint_sha256: Sha256
    policy_version: PolicyVersion
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_root(self) -> Self:
        _require_canonical_root_uri(self.root_uri)
        return self


class PolicyDecisionRecord(StrictProtocolModel):
    """Auditable result of intersecting policy and current grant capability."""

    decision_id: DecisionId
    outcome: DecisionOutcome
    capability: Capability
    policy_version: PolicyVersion
    matched_rule_ids: tuple[BoundedLabel, ...] = Field(min_length=1, max_length=64)
    reason: BoundedReason
    evaluated_at: UtcTimestamp
    trace: TraceLink

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        _require_canonical_values(self.matched_rule_ids, "matched_rule_ids")
        return self
