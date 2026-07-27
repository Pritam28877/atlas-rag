"""Platform isolation capability and admission tests."""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from app.services.harness.policy import (
    FilesystemOperation,
    FilesystemTarget,
    NetworkMethod,
    NetworkTarget,
)
from app.services.harness.protocol.sandbox import (
    PlatformIsolationCapabilities,
    SandboxAdmissionOutcome,
    SandboxIsolationMode,
    SandboxPlatform,
    platform_capabilities_sha256,
)
from app.services.harness.sandbox import (
    compile_sandbox_admission,
    compile_sandbox_profile,
    detect_platform_isolation,
    unsupported_platform_capabilities,
)
from tests.harness.policy.fixtures import NOW
from tests.harness.sandbox.fixtures import (
    OPERATION_ID,
    defaults,
    executable_target,
    grant,
)


def degraded_capabilities() -> PlatformIsolationCapabilities:
    values = {
        "platform": SandboxPlatform.LINUX,
        "mode": SandboxIsolationMode.READ_ONLY_DEGRADED,
        "native_backend": "bubblewrap-test",
        "containment_available": True,
        "filesystem_isolation": True,
        "process_tree_isolation": True,
        "network_isolation": True,
        "syscall_filtering": False,
        "resource_limits_enforced": False,
        "cgroup_v2": True,
        "cgroup_delegated": False,
        "proxy_egress": False,
        "side_effects_supported": False,
        "reasons": ("Synthetic degraded capability fixture.",),
        "detected_at": NOW,
    }
    return PlatformIsolationCapabilities(
        **values,
        capabilities_sha256=platform_capabilities_sha256(**values),
    )


def profile(tmp_path: Path, *, network: bool = False):
    work = tmp_path / "work"
    work.mkdir()
    grants = [
        grant(executable_target(), "executable.run"),
        grant(
            FilesystemTarget(
                root_uri="file:///workspace",
                relative_path="work",
                operation=FilesystemOperation.READ,
            ),
            "filesystem.read",
        ),
    ]
    if network:
        grants.append(
            grant(
                NetworkTarget(
                    scheme="https",
                    host="api.example.com",
                    port=443,
                    method=NetworkMethod.GET,
                ),
                "network.connect",
            )
        )
    return compile_sandbox_profile(
        operation_id=OPERATION_ID,
        workspace=tmp_path,
        grants=tuple(grants),
        default_limits=defaults(),
    )


def test_degraded_mode_admits_read_only_and_denies_side_effects(
    tmp_path: Path,
) -> None:
    capabilities = degraded_capabilities()
    sandbox_profile = profile(tmp_path)

    read_only = compile_sandbox_admission(
        capabilities,
        sandbox_profile,
        side_effecting=False,
    )
    side_effect = compile_sandbox_admission(
        capabilities,
        sandbox_profile,
        side_effecting=True,
    )

    assert read_only.outcome is SandboxAdmissionOutcome.ADMITTED
    assert read_only.mode is SandboxIsolationMode.READ_ONLY_DEGRADED
    assert side_effect.outcome is SandboxAdmissionOutcome.DENIED
    assert side_effect.decision_sha256


def test_proxy_and_mutated_capability_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    capabilities = degraded_capabilities()
    network_profile = profile(tmp_path, network=True)
    proxy_decision = compile_sandbox_admission(
        capabilities,
        network_profile,
        side_effecting=False,
    )
    assert proxy_decision.outcome is SandboxAdmissionOutcome.DENIED
    assert proxy_decision.reason == "Proxy egress is unavailable."

    mutated = capabilities.model_copy(
        update={"capabilities_sha256": "f" * 64}
    )
    with pytest.raises(ValueError, match="hash is invalid"):
        compile_sandbox_admission(
            mutated,
            network_profile,
            side_effecting=False,
        )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_missing_linux_backend_reports_unavailable(tmp_path: Path) -> None:
    capabilities = asyncio.run(
        detect_platform_isolation(
            isolation_executable=tmp_path / "missing-bwrap",
            delegated_cgroup_root=None,
            detected_at=NOW,
        )
    )

    assert capabilities.platform is SandboxPlatform.LINUX
    assert capabilities.mode is SandboxIsolationMode.UNAVAILABLE
    assert not capabilities.side_effects_supported
    assert capabilities.reasons


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_real_linux_probe_reports_explicit_nonproduction_state() -> None:
    capabilities = asyncio.run(
        detect_platform_isolation(
            isolation_executable=Path("/usr/bin/bwrap"),
            delegated_cgroup_root=None,
            detected_at=NOW,
        )
    )

    assert capabilities.mode is SandboxIsolationMode.READ_ONLY_DEGRADED
    assert capabilities.containment_available
    assert capabilities.filesystem_isolation
    assert capabilities.process_tree_isolation
    assert capabilities.network_isolation
    assert not capabilities.syscall_filtering
    assert not capabilities.side_effects_supported


@pytest.mark.parametrize(
    ("platform", "backend"),
    (
        (SandboxPlatform.MACOS, "seatbelt"),
        (SandboxPlatform.WINDOWS, "restricted-token-job-object"),
    ),
)
def test_unimplemented_native_platforms_are_explicitly_unavailable(
    platform: SandboxPlatform,
    backend: str,
) -> None:
    capabilities = unsupported_platform_capabilities(
        platform=platform,
        native_backend=backend,
        reason="Native adapter evidence is unavailable.",
        detected_at=NOW,
    )

    assert capabilities.mode is SandboxIsolationMode.UNAVAILABLE
    assert not capabilities.side_effects_supported
