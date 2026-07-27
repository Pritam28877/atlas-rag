"""Canonical sandbox test fixtures."""

from app.services.harness.policy import (
    CapabilityLimits,
    CapabilityProposal,
    EffectivePolicyDecision,
    ExecutableTarget,
    PolicyEffect,
    PolicyLayer,
    capability_target_sha256,
)
from app.services.harness.sandbox import (
    SandboxCapabilityGrant,
    SandboxResourceLimits,
)
from tests.harness.policy.fixtures import DECISION_ID, NOW, TRACE

OPERATION_ID = "opn_" + "7" * 32
POLICY_VERSION = "pol_" + "8" * 64


def grant(target, capability: str, **limits: int) -> SandboxCapabilityGrant:
    proposal = CapabilityProposal(
        capability=capability,
        target=target,
        target_sha256=capability_target_sha256(target),
        sandbox_limits=CapabilityLimits(**limits),
    )
    decision = EffectivePolicyDecision(
        decision_id=DECISION_ID,
        effect=PolicyEffect.ALLOW,
        capability=capability,
        capability_kind=target.kind,
        request_sha256=proposal.target_sha256,
        policy_bundle_version=POLICY_VERSION,
        layer_decisions=(
            {
                "layer": PolicyLayer.SYSTEM,
                "policy_version": POLICY_VERSION,
                "effect": PolicyEffect.ALLOW,
                "matched_rule_ids": ("allow",),
                "used_default": False,
            },
        ),
        evaluated_rules=(),
        effective_limits=CapabilityLimits(**limits),
        evaluated_at=NOW,
        trace=TRACE,
    )
    return SandboxCapabilityGrant(proposal=proposal, decision=decision)


def executable_target() -> ExecutableTarget:
    return ExecutableTarget(
        executable="/usr/bin/python3",
        arguments=("-c", "print('safe')"),
        working_directory="work",
    )


def defaults() -> SandboxResourceLimits:
    return SandboxResourceLimits(
        wall_time_ms=10_000,
        cpu_time_ms=5_000,
        memory_bytes=512 * 1024 * 1024,
        output_bytes=1_048_576,
        processes=8,
    )
