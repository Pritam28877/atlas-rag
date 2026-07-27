"""Platform isolation capability and admission wire contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    BoundedReason,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)


class SandboxPlatform(StrEnum):
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    OTHER = "other"


class SandboxIsolationMode(StrEnum):
    PRODUCTION = "production"
    READ_ONLY_DEGRADED = "read_only_degraded"
    UNAVAILABLE = "unavailable"


class SandboxAdmissionOutcome(StrEnum):
    ADMITTED = "admitted"
    DENIED = "denied"


class PlatformIsolationCapabilities(StrictProtocolModel):
    platform: SandboxPlatform
    mode: SandboxIsolationMode
    native_backend: BoundedLabel
    containment_available: bool
    filesystem_isolation: bool
    process_tree_isolation: bool
    network_isolation: bool
    syscall_filtering: bool
    resource_limits_enforced: bool
    cgroup_v2: bool
    cgroup_delegated: bool
    proxy_egress: bool
    side_effects_supported: bool
    reasons: tuple[BoundedReason, ...] = Field(max_length=16)
    detected_at: UtcTimestamp
    capabilities_sha256: Sha256

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if tuple(sorted(set(self.reasons))) != self.reasons:
            raise ValueError("isolation capability reasons must be unique and sorted")
        required_controls = (
            self.containment_available,
            self.filesystem_isolation,
            self.process_tree_isolation,
            self.network_isolation,
            self.syscall_filtering,
            self.resource_limits_enforced,
        )
        if self.mode is SandboxIsolationMode.PRODUCTION and (
            not all(required_controls) or not self.side_effects_supported
        ):
            raise ValueError("production isolation requires every core control")
        if self.mode is SandboxIsolationMode.READ_ONLY_DEGRADED and (
            not all(required_controls[:4]) or self.side_effects_supported
        ):
            raise ValueError("degraded isolation must be contained and read-only")
        if (
            self.mode is SandboxIsolationMode.UNAVAILABLE
            and self.side_effects_supported
        ):
            raise ValueError("unavailable isolation cannot support side effects")
        if self.cgroup_delegated and (
            not self.cgroup_v2 or not self.resource_limits_enforced
        ):
            raise ValueError(
                "cgroup delegation requires v2 resource enforcement"
            )
        expected = platform_capabilities_sha256(
            **self.model_dump(
                exclude={"capabilities_sha256"},
            )
        )
        if self.capabilities_sha256 != expected:
            raise ValueError("isolation capability hash is invalid")
        return self


class SandboxAdmissionDecision(StrictProtocolModel):
    outcome: SandboxAdmissionOutcome
    platform: SandboxPlatform
    mode: SandboxIsolationMode
    capabilities_sha256: Sha256
    profile_sha256: Sha256
    side_effecting: bool
    reason: BoundedReason
    decision_sha256: Sha256

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.outcome is SandboxAdmissionOutcome.ADMITTED and (
            self.mode is SandboxIsolationMode.UNAVAILABLE
            or (
                self.side_effecting
                and self.mode is not SandboxIsolationMode.PRODUCTION
            )
        ):
            raise ValueError("sandbox admission contradicts isolation mode")
        expected = sandbox_admission_sha256(
            **self.model_dump(exclude={"decision_sha256"})
        )
        if self.decision_sha256 != expected:
            raise ValueError("sandbox admission decision hash is invalid")
        return self


def platform_capabilities_sha256(**values: object) -> str:
    return _canonical_sha256(values)


def sandbox_admission_sha256(**values: object) -> str:
    return _canonical_sha256(values)


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
