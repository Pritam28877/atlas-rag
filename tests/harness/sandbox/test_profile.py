"""Exact sandbox profile compilation tests."""

from pathlib import Path

import pytest

from app.services.harness.policy import (
    CapabilityKind,
    CapabilityLimits,
    CapabilityProposal,
    EffectivePolicyDecision,
    ExecutableTarget,
    FilesystemOperation,
    FilesystemTarget,
    NetworkMethod,
    NetworkTarget,
    PolicyEffect,
    PolicyLayer,
    SecretTarget,
    capability_target_sha256,
)
from app.services.harness.protocol.secret_delivery import (
    ChildSecretBinding,
    SecretInjectionRequest,
)
from app.services.harness.sandbox import (
    SandboxCapabilityGrant,
    SandboxMount,
    SandboxMountAccess,
    SandboxNetworkMode,
    SandboxProfileError,
    SandboxResourceLimits,
    compile_sandbox_profile,
    sandbox_destination_sha256,
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


def test_profile_contains_only_exact_mounts_and_strictest_limits(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    input_file = work / "input.txt"
    input_file.write_text("input", encoding="utf-8")
    grants = (
        grant(executable_target(), "executable.run", max_duration_ms=8_000),
        grant(
            FilesystemTarget(
                root_uri="file:///workspace",
                relative_path="work",
                operation=FilesystemOperation.WRITE,
            ),
            "filesystem.write",
            max_output_bytes=2_000,
        ),
        grant(
            NetworkTarget(
                scheme="https",
                host="api.example.com",
                port=443,
                method=NetworkMethod.POST,
            ),
            "network.connect",
        ),
    )

    profile = compile_sandbox_profile(
        operation_id=OPERATION_ID,
        workspace=tmp_path,
        grants=grants,
        default_limits=defaults(),
    )

    assert profile.mounts[0].source == work
    assert profile.mounts[0].destination == "/workspace/work"
    assert profile.mounts[0].access is SandboxMountAccess.READ_WRITE
    assert profile.network_mode is SandboxNetworkMode.PROXY
    assert profile.network_rules[0].host == "api.example.com"
    assert profile.limits.wall_time_ms == 8_000
    assert profile.limits.output_bytes == 2_000
    assert profile.profile_sha256
    assert input_file.read_text(encoding="utf-8") == "input"


def test_denied_or_mismatched_decision_cannot_become_grant() -> None:
    allowed = grant(executable_target(), "executable.run")
    for update in (
        {"effect": PolicyEffect.DENY},
        {"request_sha256": "f" * 64},
        {"capability_kind": CapabilityKind.NETWORK},
    ):
        decision = allowed.decision.model_copy(update=update)
        with pytest.raises(ValueError, match="not an exact allow"):
            SandboxCapabilityGrant(
                proposal=allowed.proposal,
                decision=decision,
            )


def test_profile_contract_rejects_mount_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside workspace"):
        SandboxMount(
            source=tmp_path,
            destination="/workspace/../host",
            access=SandboxMountAccess.READ_ONLY,
        )


def test_workspace_escape_and_overlapping_mounts_fail_closed(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    nested = work / "nested"
    nested.mkdir()
    grants = (
        grant(executable_target(), "executable.run"),
        grant(
            FilesystemTarget(
                root_uri="file:///workspace",
                relative_path="work",
                operation=FilesystemOperation.READ,
            ),
            "filesystem.read",
        ),
        grant(
            FilesystemTarget(
                root_uri="file:///workspace",
                relative_path="work/nested",
                operation=FilesystemOperation.WRITE,
            ),
            "filesystem.write",
        ),
    )
    with pytest.raises(SandboxProfileError, match="overlapping"):
        compile_sandbox_profile(
            operation_id=OPERATION_ID,
            workspace=tmp_path,
            grants=grants,
            default_limits=defaults(),
        )

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "escape"
    link.symlink_to(outside, target_is_directory=True)
    escape_grants = (
        grant(executable_target(), "executable.run"),
        grant(
            FilesystemTarget(
                root_uri="file:///workspace",
                relative_path="escape",
                operation=FilesystemOperation.READ,
            ),
            "filesystem.read",
        ),
    )
    with pytest.raises(SandboxProfileError, match="escapes"):
        compile_sandbox_profile(
            operation_id=OPERATION_ID,
            workspace=tmp_path,
            grants=escape_grants,
            default_limits=defaults(),
        )


def test_secret_environment_requires_exact_destination_capability(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    executable = executable_target()
    destination = sandbox_destination_sha256(
        operation_id=OPERATION_ID,
        executable=executable,
    )
    secret_request = SecretInjectionRequest(
        operation_id=OPERATION_ID,
        destination_sha256=destination,
        bindings=(
            ChildSecretBinding(
                secret_handle="secret:provider/api-key",
                environment_name="PROVIDER_API_KEY",
            ),
        ),
        inherited_environment_names=("LANG",),
    )
    grants = (
        grant(executable, "executable.run"),
        grant(
            FilesystemTarget(
                root_uri="file:///workspace",
                relative_path="work",
                operation=FilesystemOperation.READ,
            ),
            "filesystem.read",
        ),
        grant(
            SecretTarget(
                secret_handle="secret:provider/api-key",
                destination_sha256=destination,
            ),
            "secret.read",
        ),
    )

    profile = compile_sandbox_profile(
        operation_id=OPERATION_ID,
        workspace=tmp_path,
        grants=grants,
        default_limits=defaults(),
        secret_request=secret_request,
    )
    assert profile.inherited_environment_names == ("LANG",)
    assert profile.secret_environment_names == ("PROVIDER_API_KEY",)

    unauthorized = grants[:-1]
    with pytest.raises(SandboxProfileError, match="not capability-authorized"):
        compile_sandbox_profile(
            operation_id=OPERATION_ID,
            workspace=tmp_path,
            grants=unauthorized,
            default_limits=defaults(),
            secret_request=secret_request,
        )
