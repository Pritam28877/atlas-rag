"""Disposable real-Linux escape and exfiltration probes."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from app.services.harness.policy import (
    ExecutableTarget,
    FilesystemOperation,
    FilesystemTarget,
    SecretTarget,
)
from app.services.harness.protocol.sandbox import SandboxAdmissionOutcome
from app.services.harness.protocol.secret_delivery import (
    ChildSecretBinding,
    SecretDeliveryReceipt,
    SecretInjectionRequest,
    secret_delivery_receipt_sha256,
    secret_handle_sha256,
)
from app.services.harness.sandbox import (
    SandboxChildEnvironment,
    SandboxSupervisor,
    compile_sandbox_admission,
    compile_sandbox_profile,
    detect_platform_isolation,
    sandbox_destination_sha256,
)
from app.services.harness.sandbox.linux import LinuxBubblewrapCommandBuilder
from tests.harness.policy.fixtures import NOW
from tests.harness.sandbox.fixtures import (
    OPERATION_ID,
    defaults,
    grant,
)


class Lease:
    async def kill(self) -> None:
        return None

    async def close(self) -> None:
        return None


class Controller:
    async def admit(self, process_id, limits):
        return Lease()


def escape_profile(
    workspace: Path,
    *,
    child_code: str,
):
    declared = workspace / "declared"
    declared.mkdir(parents=True)
    (declared / "input.txt").write_text("allowed", encoding="utf-8")
    (declared / "probe.py").write_text(child_code, encoding="utf-8")
    executable = ExecutableTarget(
        executable="/usr/bin/python3",
        arguments=("/workspace/declared/probe.py",),
        working_directory="declared",
    )
    return compile_sandbox_profile(
        operation_id=OPERATION_ID,
        workspace=workspace,
        grants=(
            grant(executable, "executable.run"),
            grant(
                FilesystemTarget(
                    root_uri="file:///workspace",
                    relative_path="declared",
                    operation=FilesystemOperation.READ,
                ),
                "filesystem.read",
            ),
        ),
        default_limits=defaults(),
    )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_real_read_only_sandbox_blocks_escape_corpus(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_text("synthetic-host-canary", encoding="utf-8")
    child_code = f"""
import json
import os
import socket
from pathlib import Path

def write_is_denied(path):
    try:
        path.write_text("blocked")
    except OSError:
        return True
    return False

network_denied = False
connection = socket.socket()
connection.settimeout(0.2)
try:
    connection.connect(("1.1.1.1", 53))
except OSError:
    network_denied = True
finally:
    connection.close()

checks = {{
    "declared_read": Path("/workspace/declared/input.txt").read_text() == "allowed",
    "declared_write_denied": write_is_denied(
        Path("/workspace/declared/mutate.txt")
    ),
    "environment_filtered": "ATLAS_SECURITY_CANARY" not in os.environ,
    "host_block_device_hidden": not Path("/dev/sda").exists(),
    "host_path_hidden": not Path({str(host_secret)!r}).exists(),
    "network_denied": network_denied,
    "undeclared_write_denied": write_is_denied(
        Path("/workspace/blocked.txt")
    ),
}}
print(json.dumps(checks, sort_keys=True))
"""
    profile = escape_profile(workspace, child_code=child_code)

    async def scenario():
        capabilities = await detect_platform_isolation(
            isolation_executable=Path("/usr/bin/bwrap"),
            delegated_cgroup_root=None,
            detected_at=NOW,
        )
        admission = compile_sandbox_admission(
            capabilities,
            profile,
            side_effecting=False,
        )
        assert admission.outcome is SandboxAdmissionOutcome.ADMITTED
        return await SandboxSupervisor(
            LinuxBubblewrapCommandBuilder(Path("/usr/bin/bwrap")),
            Controller(),
        ).run(
            profile,
            SandboxChildEnvironment(
                operation_id=profile.operation_id,
                destination_sha256=profile.destination_sha256,
                parent_environment={},
                secret_environment={},
                receipt=None,
            ),
            cancellation=asyncio.Event(),
        )

    result = asyncio.run(scenario())
    checks = json.loads(result.stdout)

    assert result.return_code == 0, result.stderr
    assert checks and all(checks.values())
    assert not (workspace / "blocked.txt").exists()
    assert not (workspace / "declared/mutate.txt").exists()
    assert host_secret.read_text(encoding="utf-8") == "synthetic-host-canary"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_secret_profile_is_denied_and_output_is_defense_in_depth_redacted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    declared = workspace / "declared"
    declared.mkdir(parents=True)
    script = declared / "print_secret.py"
    script.write_text(
        "import os; print(os.environ['PROVIDER_API_KEY'])\n",
        encoding="utf-8",
    )
    executable = ExecutableTarget(
        executable="/usr/bin/python3",
        arguments=("/workspace/declared/print_secret.py",),
        working_directory="declared",
    )
    destination = sandbox_destination_sha256(
        operation_id=OPERATION_ID,
        executable=executable,
    )
    handle = "secret:provider/security-canary"
    secret_request = SecretInjectionRequest(
        operation_id=OPERATION_ID,
        destination_sha256=destination,
        bindings=(
            ChildSecretBinding(
                secret_handle=handle,
                environment_name="PROVIDER_API_KEY",
            ),
        ),
        inherited_environment_names=(),
    )
    profile = compile_sandbox_profile(
        operation_id=OPERATION_ID,
        workspace=workspace,
        grants=(
            grant(executable, "executable.run"),
            grant(
                FilesystemTarget(
                    root_uri="file:///workspace",
                    relative_path="declared",
                    operation=FilesystemOperation.READ,
                ),
                "filesystem.read",
            ),
            grant(
                SecretTarget(
                    secret_handle=handle,
                    destination_sha256=destination,
                ),
                "secret.read",
            ),
        ),
        default_limits=defaults(),
        secret_request=secret_request,
    )
    receipt_values = {
        "operation_id": OPERATION_ID,
        "destination_sha256": destination,
        "secret_handle_sha256s": (secret_handle_sha256(handle),),
        "environment_names": ("PROVIDER_API_KEY",),
        "delivered_at": NOW,
    }
    receipt = SecretDeliveryReceipt(
        **receipt_values,
        receipt_sha256=secret_delivery_receipt_sha256(**receipt_values),
    )
    secret = bytearray(b"synthetic-secret")
    environment = SandboxChildEnvironment(
        operation_id=profile.operation_id,
        destination_sha256=profile.destination_sha256,
        parent_environment={},
        secret_environment={
            "PROVIDER_API_KEY": memoryview(secret).toreadonly(),
        },
        receipt=receipt,
    )

    async def scenario():
        capabilities = await detect_platform_isolation(
            isolation_executable=Path("/usr/bin/bwrap"),
            delegated_cgroup_root=None,
            detected_at=NOW,
        )
        admission = compile_sandbox_admission(
            capabilities,
            profile,
            side_effecting=False,
        )
        assert admission.outcome is SandboxAdmissionOutcome.DENIED
        assert admission.side_effecting
        result = await SandboxSupervisor(
            LinuxBubblewrapCommandBuilder(Path("/usr/bin/bwrap")),
            Controller(),
        ).run(
            profile,
            environment,
            cancellation=asyncio.Event(),
        )
        return result

    result = asyncio.run(scenario())
    assert result.return_code == 0, result.stderr
    assert b"synthetic-secret" not in result.stdout
    assert result.stdout == b"****************\n"
