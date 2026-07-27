"""Active platform isolation detection and fail-closed profile admission."""

from __future__ import annotations

import asyncio
import os
import platform as system_platform
from datetime import datetime
from pathlib import Path

from app.services.harness.protocol.sandbox import (
    PlatformIsolationCapabilities,
    SandboxIsolationMode,
    SandboxPlatform,
    platform_capabilities_sha256,
)
from app.services.harness.sandbox.process_control import terminate_process_tree

CAPABILITY_PROBE_TIMEOUT_SECONDS = 3.0
CGROUP_ROOT = Path("/sys/fs/cgroup")
REQUIRED_CGROUP_CONTROLLERS = frozenset({"cpu", "memory", "pids"})


async def detect_platform_isolation(
    *,
    isolation_executable: Path | None,
    delegated_cgroup_root: Path | None,
    detected_at: datetime,
) -> PlatformIsolationCapabilities:
    platform_name = system_platform.system().lower()
    if platform_name == "linux":
        return await _detect_linux(
            isolation_executable=isolation_executable,
            delegated_cgroup_root=delegated_cgroup_root,
            detected_at=detected_at,
        )
    if platform_name == "darwin":
        return unsupported_platform_capabilities(
            platform=SandboxPlatform.MACOS,
            native_backend="seatbelt",
            reason="Seatbelt adapter and native escape evidence are unavailable.",
            detected_at=detected_at,
        )
    if platform_name == "windows":
        return unsupported_platform_capabilities(
            platform=SandboxPlatform.WINDOWS,
            native_backend="restricted-token-job-object",
            reason=(
                "Restricted-token and Job Object adapters are unavailable."
            ),
            detected_at=detected_at,
        )
    return unsupported_platform_capabilities(
        platform=SandboxPlatform.OTHER,
        native_backend="unsupported-platform",
        reason="No isolation backend exists for this platform.",
        detected_at=detected_at,
    )


def unsupported_platform_capabilities(
    *,
    platform: SandboxPlatform,
    native_backend: str,
    reason: str,
    detected_at: datetime,
) -> PlatformIsolationCapabilities:
    return _build_capabilities(
        platform=platform,
        mode=SandboxIsolationMode.UNAVAILABLE,
        native_backend=native_backend,
        containment_available=False,
        filesystem_isolation=False,
        process_tree_isolation=False,
        network_isolation=False,
        syscall_filtering=False,
        resource_limits_enforced=False,
        cgroup_v2=False,
        cgroup_delegated=False,
        proxy_egress=False,
        side_effects_supported=False,
        reasons=(reason,),
        detected_at=detected_at,
    )


async def _detect_linux(
    *,
    isolation_executable: Path | None,
    delegated_cgroup_root: Path | None,
    detected_at: datetime,
) -> PlatformIsolationCapabilities:
    unavailable_reasons: list[str] = []
    executable = _resolve_executable(isolation_executable)
    user_namespaces = _user_namespaces_available()
    backend_label = "bubblewrap-unavailable"
    bubblewrap_works = False
    if executable is None:
        unavailable_reasons.append(
            "Configured bubblewrap executable is unavailable."
        )
    elif not user_namespaces:
        unavailable_reasons.append(
            "Unprivileged user namespaces are unavailable."
        )
    else:
        backend_label, bubblewrap_works = await _probe_bubblewrap(executable)
        if not bubblewrap_works:
            unavailable_reasons.append(
                "Bubblewrap namespace probe failed."
            )

    cgroup_v2 = (CGROUP_ROOT / "cgroup.controllers").is_file()
    cgroup_delegated = _cgroup_is_delegated(
        delegated_cgroup_root,
        cgroup_v2=cgroup_v2,
    )
    if unavailable_reasons:
        return _build_capabilities(
            platform=SandboxPlatform.LINUX,
            mode=SandboxIsolationMode.UNAVAILABLE,
            native_backend=backend_label,
            containment_available=False,
            filesystem_isolation=False,
            process_tree_isolation=False,
            network_isolation=False,
            syscall_filtering=False,
            resource_limits_enforced=False,
            cgroup_v2=cgroup_v2,
            cgroup_delegated=cgroup_delegated,
            proxy_egress=False,
            side_effects_supported=False,
            reasons=tuple(unavailable_reasons),
            detected_at=detected_at,
        )

    degraded_reasons = [
        "Seccomp policy attachment is not implemented.",
        "Proxy-mediated egress attachment is not implemented.",
    ]
    if not cgroup_delegated:
        degraded_reasons.append(
            "A writable delegated cgroup v2 scope is unavailable."
        )
    return _build_capabilities(
        platform=SandboxPlatform.LINUX,
        mode=SandboxIsolationMode.READ_ONLY_DEGRADED,
        native_backend=backend_label,
        containment_available=True,
        filesystem_isolation=True,
        process_tree_isolation=True,
        network_isolation=True,
        syscall_filtering=False,
        resource_limits_enforced=cgroup_delegated,
        cgroup_v2=cgroup_v2,
        cgroup_delegated=cgroup_delegated,
        proxy_egress=False,
        side_effects_supported=False,
        reasons=tuple(degraded_reasons),
        detected_at=detected_at,
    )


def _resolve_executable(path: Path | None) -> Path | None:
    if path is None or not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return resolved


async def _probe_bubblewrap(executable: Path) -> tuple[str, bool]:
    try:
        version_process = await asyncio.create_subprocess_exec(
            executable.as_posix(),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
    except OSError:
        return "bubblewrap-spawn-failed", False
    if version_process.stdout is None:
        await terminate_process_tree(version_process, None)
        return "bubblewrap-version-pipe-failed", False
    try:
        stdout = await asyncio.wait_for(
            version_process.stdout.read(129),
            timeout=CAPABILITY_PROBE_TIMEOUT_SECONDS,
        )
        return_code = await asyncio.wait_for(
            version_process.wait(),
            timeout=CAPABILITY_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        await terminate_process_tree(version_process, None)
        return "bubblewrap-probe-timeout", False
    except asyncio.CancelledError:
        await terminate_process_tree(version_process, None)
        raise
    if return_code != 0 or len(stdout) > 128:
        return "bubblewrap-version-failed", False
    label = stdout.decode("utf-8", errors="replace").strip()
    if not label or len(label) > 128 or any(
        ord(character) < 32 or ord(character) == 127
        for character in label
    ):
        label = "bubblewrap-version-invalid"

    command = [
        executable.as_posix(),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
    ]
    for runtime_mount in (Path("/usr"), Path("/lib"), Path("/lib64")):
        if runtime_mount.exists():
            command.extend(
                [
                    "--ro-bind",
                    runtime_mount.as_posix(),
                    runtime_mount.as_posix(),
                ]
            )
    command.extend(
        [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--clearenv",
            "/usr/bin/true",
        ]
    )
    try:
        probe_process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
    except OSError:
        return label, False
    try:
        return_code = await asyncio.wait_for(
            probe_process.wait(),
            timeout=CAPABILITY_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        await terminate_process_tree(probe_process, None)
        return label, False
    except asyncio.CancelledError:
        await terminate_process_tree(probe_process, None)
        raise
    return label, return_code == 0


def _user_namespaces_available() -> bool:
    maximum = _read_text(Path("/proc/sys/user/max_user_namespaces"))
    if maximum is not None:
        try:
            if int(maximum) <= 0:
                return False
        except ValueError:
            return False
    clone_flag = _read_text(
        Path("/proc/sys/kernel/unprivileged_userns_clone")
    )
    return clone_flag != "0"


def _cgroup_is_delegated(path: Path | None, *, cgroup_v2: bool) -> bool:
    if path is None or not cgroup_v2 or not path.is_absolute():
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    if resolved == CGROUP_ROOT or not resolved.is_relative_to(CGROUP_ROOT):
        return False
    controllers = _read_text(resolved / "cgroup.controllers")
    if controllers is None:
        return False
    available = frozenset(controllers.split())
    return REQUIRED_CGROUP_CONTROLLERS.issubset(available) and os.access(
        resolved,
        os.W_OK,
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _build_capabilities(
    **values: object,
) -> PlatformIsolationCapabilities:
    normalized = dict(values)
    reasons = normalized.get("reasons")
    if isinstance(reasons, tuple):
        normalized["reasons"] = tuple(sorted(set(reasons)))
    return PlatformIsolationCapabilities.model_validate(
        {
            **normalized,
            "capabilities_sha256": platform_capabilities_sha256(
                **normalized
            ),
        }
    )
