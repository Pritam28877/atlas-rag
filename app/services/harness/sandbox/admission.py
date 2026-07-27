"""Fail-closed platform capability and sandbox profile intersection."""

from app.services.harness.protocol.sandbox import (
    PlatformIsolationCapabilities,
    SandboxAdmissionDecision,
    SandboxAdmissionOutcome,
    SandboxIsolationMode,
    sandbox_admission_sha256,
)
from app.services.harness.sandbox.models import (
    SandboxMountAccess,
    SandboxNetworkMode,
    SandboxProfile,
)


def compile_sandbox_admission(
    capabilities: PlatformIsolationCapabilities,
    profile: SandboxProfile,
    *,
    side_effecting: bool,
) -> SandboxAdmissionDecision:
    validated_capabilities = PlatformIsolationCapabilities.model_validate(
        capabilities.model_dump()
    )
    validated_profile = SandboxProfile.model_validate(profile.model_dump())
    profile_side_effecting = (
        side_effecting
        or bool(validated_profile.secret_environment_names)
        or validated_profile.network_mode is SandboxNetworkMode.PROXY
        or any(
            mount.access is SandboxMountAccess.READ_WRITE
            for mount in validated_profile.mounts
        )
    )
    outcome = SandboxAdmissionOutcome.ADMITTED
    reason = "Sandbox profile admitted with production isolation."
    if validated_capabilities.mode is SandboxIsolationMode.UNAVAILABLE:
        outcome = SandboxAdmissionOutcome.DENIED
        reason = "Platform isolation is unavailable."
    elif (
        validated_profile.network_mode is SandboxNetworkMode.PROXY
        and not validated_capabilities.proxy_egress
    ):
        outcome = SandboxAdmissionOutcome.DENIED
        reason = "Proxy egress is unavailable."
    elif (
        profile_side_effecting
        and validated_capabilities.mode is not SandboxIsolationMode.PRODUCTION
    ):
        outcome = SandboxAdmissionOutcome.DENIED
        reason = "Side effects require production isolation."
    elif (
        validated_capabilities.mode
        is SandboxIsolationMode.READ_ONLY_DEGRADED
    ):
        reason = "Read-only profile admitted with explicitly degraded isolation."

    values: dict[str, object] = {
        "outcome": outcome,
        "platform": validated_capabilities.platform,
        "mode": validated_capabilities.mode,
        "capabilities_sha256": validated_capabilities.capabilities_sha256,
        "profile_sha256": validated_profile.profile_sha256,
        "side_effecting": profile_side_effecting,
        "reason": reason,
    }
    return SandboxAdmissionDecision.model_validate(
        {
            **values,
            "decision_sha256": sandbox_admission_sha256(**values),
        }
    )
