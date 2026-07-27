"""Bounded immutable sandbox profile contracts."""

import hashlib
import json
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.policy import (
    CapabilityProposal,
    EffectivePolicyDecision,
    PolicyEffect,
)
from app.services.harness.protocol import OperationId, Sha256, StrictProtocolModel
from app.services.harness.protocol.secret_delivery import ChildEnvironmentName

type SandboxArgument = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512, pattern=r"^[^\x00]+$"),
]


class SandboxMountAccess(StrEnum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class SandboxNetworkMode(StrEnum):
    ISOLATED = "isolated"
    PROXY = "proxy"


class SandboxCapabilityGrant(StrictProtocolModel):
    proposal: CapabilityProposal
    decision: EffectivePolicyDecision

    @model_validator(mode="after")
    def validate_exact_allow(self) -> Self:
        if (
            self.decision.effect is not PolicyEffect.ALLOW
            or self.decision.capability != self.proposal.capability
            or self.decision.capability_kind is not self.proposal.target.kind
            or self.decision.request_sha256 != self.proposal.target_sha256
        ):
            raise ValueError("sandbox capability grant is not an exact allow")
        return self


class SandboxMount(StrictProtocolModel):
    source: Path
    destination: str = Field(pattern=r"^/(?:[^/\x00]+/?)*$")
    access: SandboxMountAccess

    @model_validator(mode="after")
    def validate_absolute_source(self) -> Self:
        if not self.source.is_absolute():
            raise ValueError("sandbox mount source must be absolute")
        destination = PurePosixPath(self.destination)
        workspace = PurePosixPath("/workspace")
        if (
            destination.as_posix() != self.destination
            or destination == workspace
            or not destination.is_relative_to(workspace)
            or ".." in destination.parts
        ):
            raise ValueError("sandbox mount destination must be inside workspace")
        return self


class SandboxNetworkRule(StrictProtocolModel):
    scheme: Literal["http", "https"]
    host: str
    port: int = Field(ge=1, le=65_535)
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    follow_redirects: bool

    @model_validator(mode="after")
    def validate_host(self) -> Self:
        if (
            self.host != self.host.lower()
            or not 1 <= len(self.host) <= 253
            or "://" in self.host
            or any(character.isspace() for character in self.host)
            or self.host.startswith(".")
            or self.host.endswith(".")
        ):
            raise ValueError("sandbox network host is not canonical")
        return self


class SandboxResourceLimits(StrictProtocolModel):
    wall_time_ms: int = Field(ge=100, le=3_600_000)
    cpu_time_ms: int = Field(ge=100, le=3_600_000)
    memory_bytes: int = Field(ge=16 * 1024 * 1024, le=64 * 1024**3)
    output_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    processes: int = Field(ge=1, le=1_024)


class SandboxProfile(StrictProtocolModel):
    operation_id: OperationId
    executable_source: Path
    executable: str
    arguments: tuple[SandboxArgument, ...] = Field(max_length=64)
    working_directory: str
    mounts: tuple[SandboxMount, ...] = Field(max_length=64)
    network_mode: SandboxNetworkMode
    network_rules: tuple[SandboxNetworkRule, ...] = Field(max_length=64)
    inherited_environment_names: tuple[ChildEnvironmentName, ...] = Field(
        max_length=64
    )
    secret_environment_names: tuple[ChildEnvironmentName, ...] = Field(
        max_length=16
    )
    limits: SandboxResourceLimits
    destination_sha256: Sha256
    profile_sha256: Sha256

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        executable = PurePosixPath(self.executable)
        working_directory = PurePosixPath(self.working_directory)
        if (
            not executable.is_absolute()
            or executable.as_posix() != self.executable
            or ".." in executable.parts
            or not self.executable_source.is_absolute()
        ):
            raise ValueError("sandbox executable paths are not canonical")
        if (
            working_directory.as_posix() != self.working_directory
            or not working_directory.is_relative_to(PurePosixPath("/workspace"))
            or ".." in working_directory.parts
        ):
            raise ValueError("sandbox working directory is not canonical")
        mount_keys = tuple(
            (mount.destination, mount.access.value) for mount in self.mounts
        )
        if tuple(sorted(set(mount_keys))) != mount_keys:
            raise ValueError("sandbox mounts must be unique and sorted")
        network_keys = tuple(
            (
                rule.scheme,
                rule.host,
                rule.port,
                rule.method,
                rule.follow_redirects,
            )
            for rule in self.network_rules
        )
        if tuple(sorted(set(network_keys))) != network_keys:
            raise ValueError("sandbox network rules must be unique and sorted")
        if tuple(sorted(set(self.inherited_environment_names))) != (
            self.inherited_environment_names
        ):
            raise ValueError("inherited environment names must be unique and sorted")
        if tuple(sorted(set(self.secret_environment_names))) != (
            self.secret_environment_names
        ):
            raise ValueError("secret environment names must be unique and sorted")
        if set(self.inherited_environment_names).intersection(
            self.secret_environment_names
        ):
            raise ValueError("sandbox environment scopes must not overlap")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"profile_sha256"})
        )
        if self.profile_sha256 != expected:
            raise ValueError("sandbox profile hash is invalid")
        return self


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda item: item.as_posix() if isinstance(item, Path) else item,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
