"""Fail-closed bubblewrap command construction for Linux profiles."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from app.services.harness.sandbox.models import (
    SandboxMountAccess,
    SandboxNetworkMode,
    SandboxProfile,
)

DEFAULT_RUNTIME_MOUNTS = (
    Path("/usr/lib"),
    Path("/lib"),
    Path("/lib64"),
    Path("/etc/ld.so.cache"),
)
_RUNTIME_DIRECTORY_ROOTS = (
    PurePosixPath("/usr/lib"),
    PurePosixPath("/usr/lib64"),
    PurePosixPath("/lib"),
    PurePosixPath("/lib64"),
)
_RUNTIME_FILES = {PurePosixPath("/etc/ld.so.cache")}
SANDBOX_ADMISSION_FIFO = "/run/atlas/admission"
SANDBOX_SECRET_FIFO = "/run/atlas/secrets"

_LAUNCHER_CODE = """
import os
import struct
import sys

def read_exact(stream, size):
    value = stream.read(size)
    if len(value) != size:
        raise RuntimeError("sandbox environment packet is incomplete")
    return value

with open("/run/atlas/admission", "rb", buffering=0) as admission:
    if read_exact(admission, 1) != b"1":
        raise RuntimeError("sandbox admission was denied")

environment = dict(os.environ)
with open("/run/atlas/secrets", "rb", buffering=0) as secrets:
    count = struct.unpack(">H", read_exact(secrets, 2))[0]
    for _ in range(count):
        name_size = struct.unpack(">H", read_exact(secrets, 2))[0]
        value_size = struct.unpack(">I", read_exact(secrets, 4))[0]
        name = read_exact(secrets, name_size).decode("ascii")
        value = read_exact(secrets, value_size).decode("utf-8")
        environment[name] = value

os.execve(sys.argv[1], sys.argv[1:], environment)
""".strip()


class LinuxSandboxCommandError(RuntimeError):
    """A profile cannot be represented by the Linux sandbox backend."""


class LinuxBubblewrapCommandBuilder:
    def __init__(
        self,
        isolation_executable: Path,
        *,
        trusted_python: Path = Path("/usr/bin/python3"),
        runtime_mounts: tuple[Path, ...] = DEFAULT_RUNTIME_MOUNTS,
    ) -> None:
        self._isolation_executable = _resolve_executable(
            isolation_executable,
            "isolation executable",
        )
        self._trusted_python = _resolve_executable(
            trusted_python,
            "trusted Python",
        )
        self._runtime_mounts = _resolve_runtime_mounts(runtime_mounts)

    def build(
        self,
        profile: SandboxProfile,
        *,
        admission_fifo: Path,
        secret_fifo: Path,
        parent_environment: Mapping[str, str],
    ) -> list[str]:
        if profile.network_mode is not SandboxNetworkMode.ISOLATED:
            raise LinuxSandboxCommandError(
                "proxy network profile has no enforceable attachment"
            )
        _require_unchanged_path(
            profile.executable_source,
            field="profile executable",
            executable=True,
        )
        for mount in profile.mounts:
            _require_unchanged_path(mount.source, field="profile mount")
        _require_fifo(admission_fifo, "admission FIFO")
        _require_fifo(secret_fifo, "secret FIFO")
        if set(parent_environment) != set(profile.inherited_environment_names):
            raise LinuxSandboxCommandError(
                "parent environment does not match sandbox profile"
            )

        directories = {
            "/workspace",
            "/run",
            "/run/atlas",
            "/tmp",
            *_destination_parents(profile.executable),
            *_destination_parents(self._trusted_python.as_posix()),
        }
        for _, destination in self._runtime_mounts:
            directories.update(_destination_parents(destination.as_posix()))
        for mount in profile.mounts:
            directories.update(_destination_parents(mount.destination))

        command = [
            self._isolation_executable.as_posix(),
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--hostname",
            "atlas-sandbox",
            "--clearenv",
        ]
        ordered_directories = sorted(
            directories,
            key=lambda value: (value.count("/"), value),
        )
        for directory in ordered_directories:
            command.extend(["--dir", directory])
        command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
        for source, destination in self._runtime_mounts:
            command.extend(
                ["--ro-bind", source.as_posix(), destination.as_posix()]
            )
        command.extend(
            [
                "--ro-bind",
                self._trusted_python.as_posix(),
                "/usr/bin/python3",
            ]
        )
        if (
            profile.executable != "/usr/bin/python3"
            or profile.executable_source != self._trusted_python
        ):
            command.extend(
                [
                    "--ro-bind",
                    profile.executable_source.as_posix(),
                    profile.executable,
                ]
            )
        for mount in profile.mounts:
            bind_option = (
                "--bind"
                if mount.access is SandboxMountAccess.READ_WRITE
                else "--ro-bind"
            )
            command.extend(
                [bind_option, mount.source.as_posix(), mount.destination]
            )
        command.extend(
            [
                "--bind",
                admission_fifo.as_posix(),
                SANDBOX_ADMISSION_FIFO,
                "--bind",
                secret_fifo.as_posix(),
                SANDBOX_SECRET_FIFO,
            ]
        )
        for name, value in sorted(parent_environment.items()):
            if "\x00" in value or len(value) > 4096:
                raise LinuxSandboxCommandError(
                    "parent environment value is invalid"
                )
            command.extend(["--setenv", name, value])
        command.extend(
            [
                "--chdir",
                profile.working_directory,
                "/usr/bin/python3",
                "-c",
                _LAUNCHER_CODE,
                profile.executable,
                *profile.arguments,
            ]
        )
        return command


def _destination_parents(destination: str) -> set[str]:
    path = PurePosixPath(destination)
    parents = {
        parent.as_posix()
        for parent in path.parents
        if parent.as_posix() != "/"
    }
    if destination.endswith("/"):
        parents.add(path.as_posix())
    return parents


def _resolve_executable(path: Path, field: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise LinuxSandboxCommandError(f"{field} is unavailable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise LinuxSandboxCommandError(f"{field} is not executable")
    return resolved


def _resolve_runtime_mounts(
    runtime_mounts: tuple[Path, ...],
) -> tuple[tuple[Path, Path], ...]:
    if len(runtime_mounts) > 16:
        raise LinuxSandboxCommandError("runtime mounts exceed their limit")
    resolved_mounts: list[tuple[Path, Path]] = []
    destinations: set[PurePosixPath] = set()
    for mount in runtime_mounts:
        destination = PurePosixPath(mount.as_posix())
        allowed = destination in _RUNTIME_FILES or any(
            destination == root or destination.is_relative_to(root)
            for root in _RUNTIME_DIRECTORY_ROOTS
        )
        if not destination.is_absolute() or not allowed:
            raise LinuxSandboxCommandError("runtime mount is outside trusted roots")
        if destination in destinations:
            raise LinuxSandboxCommandError("runtime mounts must be unique")
        destinations.add(destination)
        if mount.exists():
            resolved_mounts.append((mount.resolve(strict=True), mount))
    return tuple(resolved_mounts)


def _require_unchanged_path(
    path: Path,
    *,
    field: str,
    executable: bool = False,
) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise LinuxSandboxCommandError(f"{field} is unavailable") from error
    if resolved != path:
        raise LinuxSandboxCommandError(f"{field} changed after compilation")
    if executable and (not resolved.is_file() or not os.access(resolved, os.X_OK)):
        raise LinuxSandboxCommandError(f"{field} is not executable")


def _require_fifo(path: Path, field: str) -> None:
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise LinuxSandboxCommandError(f"{field} is unavailable") from error
    if not path.is_fifo() or mode & 0o077:
        raise LinuxSandboxCommandError(f"{field} is not private")
