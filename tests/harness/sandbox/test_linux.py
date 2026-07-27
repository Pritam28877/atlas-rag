"""Linux bubblewrap command construction tests."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from app.services.harness.policy import (
    ExecutableTarget,
    FilesystemOperation,
    FilesystemTarget,
    NetworkMethod,
    NetworkTarget,
)
from app.services.harness.sandbox import (
    SandboxChildEnvironment,
    SandboxSupervisor,
    compile_sandbox_profile,
)
from app.services.harness.sandbox.linux import (
    LinuxBubblewrapCommandBuilder,
    LinuxSandboxCommandError,
)
from tests.harness.sandbox.fixtures import (
    OPERATION_ID,
    defaults,
    grant,
)


def build_profile(
    tmp_path: Path,
    *,
    network: bool = False,
    executable: Path | None = None,
):
    work = tmp_path / "work"
    work.mkdir()
    executable = executable or Path(sys.executable)
    executable_target = ExecutableTarget(
        executable=executable.as_posix(),
        arguments=("-c", "print('safe')"),
        working_directory="work",
    )
    grants = [
        grant(executable_target, "executable.run"),
        grant(
            FilesystemTarget(
                root_uri="file:///workspace",
                relative_path="work",
                operation=FilesystemOperation.WRITE,
            ),
            "filesystem.write",
        ),
    ]
    if network:
        grants.append(
            grant(
                NetworkTarget(
                    scheme="https",
                    host="api.example.com",
                    port=443,
                    method=NetworkMethod.POST,
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


def private_fifos(tmp_path: Path) -> tuple[Path, Path]:
    admission = tmp_path / "admission"
    secrets = tmp_path / "secrets"
    os.mkfifo(admission, mode=0o600)
    os.mkfifo(secrets, mode=0o600)
    return admission, secrets


def test_command_uses_namespaces_exact_mounts_and_cleared_environment(
    tmp_path: Path,
) -> None:
    profile = build_profile(tmp_path)
    admission, secrets = private_fifos(tmp_path)
    command = LinuxBubblewrapCommandBuilder(
        Path(sys.executable),
        trusted_python=Path(sys.executable),
        runtime_mounts=(),
    ).build(
        profile,
        admission_fifo=admission,
        secret_fifo=secrets,
        parent_environment={},
    )

    assert "--unshare-all" in command
    assert "--die-with-parent" in command
    assert "--clearenv" in command
    assert "--share-net" not in command
    assert not any(
        command[index : index + 3] == ["--bind", "/", "/"]
        for index in range(len(command) - 2)
    )
    assert profile.mounts[0].source.as_posix() in command
    assert profile.executable_source.as_posix() in command
    assert "OPENAI_API_KEY" not in command


def test_proxy_profile_fails_until_enforceable_attachment_exists(
    tmp_path: Path,
) -> None:
    profile = build_profile(tmp_path, network=True)
    admission, secrets = private_fifos(tmp_path)
    builder = LinuxBubblewrapCommandBuilder(
        Path(sys.executable),
        trusted_python=Path(sys.executable),
        runtime_mounts=(),
    )

    with pytest.raises(LinuxSandboxCommandError, match="no enforceable"):
        builder.build(
            profile,
            admission_fifo=admission,
            secret_fifo=secrets,
            parent_environment={},
        )


def test_public_fifo_or_changed_mount_fails_closed(tmp_path: Path) -> None:
    profile = build_profile(tmp_path)
    admission, secrets = private_fifos(tmp_path)
    admission.chmod(0o644)
    builder = LinuxBubblewrapCommandBuilder(
        Path(sys.executable),
        trusted_python=Path(sys.executable),
        runtime_mounts=(),
    )

    with pytest.raises(LinuxSandboxCommandError, match="not private"):
        builder.build(
            profile,
            admission_fifo=admission,
            secret_fifo=secrets,
            parent_environment={},
        )

    with pytest.raises(LinuxSandboxCommandError, match="trusted roots"):
        LinuxBubblewrapCommandBuilder(
            Path(sys.executable),
            trusted_python=Path(sys.executable),
            runtime_mounts=(Path("/"),),
        )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_real_linux_builder_runs_inside_bubblewrap(tmp_path: Path) -> None:
    class Lease:
        async def kill(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class Controller:
        async def admit(self, process_id, limits):
            return Lease()

    profile = build_profile(
        tmp_path,
        executable=Path("/usr/bin/python3"),
    )
    environment = SandboxChildEnvironment(
        operation_id=profile.operation_id,
        destination_sha256=profile.destination_sha256,
        parent_environment={},
        secret_environment={},
        receipt=None,
    )
    result = asyncio.run(
        SandboxSupervisor(
            LinuxBubblewrapCommandBuilder(Path("/usr/bin/bwrap")),
            Controller(),
        ).run(
            profile,
            environment,
            cancellation=asyncio.Event(),
        )
    )

    assert result.return_code == 0
    assert result.stdout == b"safe\n"
