"""Run a disposable fail-closed Linux isolation feasibility probe."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_LIMIT = 4096
MAX_OUTPUT_LIMIT = 1_048_576
DEFAULT_TIMEOUT_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 0.5
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RUNTIME_MOUNTS = ("/usr", "/lib", "/lib64")
CHILD_PYTHON = "/usr/bin/python3"


class IsolationProbeError(RuntimeError):
    """The disposable isolation probe could not prove a required property."""


class IsolationUnavailable(IsolationProbeError):
    """The required isolation executable is unavailable."""


class OutputLimitExceeded(IsolationProbeError):
    """A child exceeded its combined output budget."""


class ProcessTimedOut(IsolationProbeError):
    """A child exceeded its wall-time budget."""


def resolve_bubblewrap(requested_path: str | None = None) -> str:
    candidate = requested_path or shutil.which("bwrap")
    if candidate is None:
        raise IsolationUnavailable("bubblewrap is required but was not found")
    executable = Path(candidate)
    if not executable.is_absolute():
        resolved = shutil.which(candidate)
        if resolved is None:
            raise IsolationUnavailable("bubblewrap is required but was not found")
        executable = Path(resolved)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise IsolationUnavailable("bubblewrap path is not an executable file")
    return str(executable)


def _read_flag(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def detect_capabilities(bubblewrap_path: str) -> dict[str, Any]:
    if not sys.platform.startswith("linux"):
        raise IsolationUnavailable("the P2 isolation probe requires Linux")
    version_result = subprocess.run(
        [bubblewrap_path, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    if version_result.returncode != 0:
        raise IsolationUnavailable("bubblewrap version check failed")

    user_namespace_flag = _read_flag(
        Path("/proc/sys/kernel/unprivileged_userns_clone")
    )
    if user_namespace_flag == "0":
        raise IsolationUnavailable("unprivileged user namespaces are disabled")
    cgroup_controllers = _read_flag(Path("/sys/fs/cgroup/cgroup.controllers"))
    return {
        "platform": "linux",
        "bubblewrap": version_result.stdout.strip(),
        "unprivileged_user_namespaces": user_namespace_flag != "0",
        "cgroup_v2": cgroup_controllers is not None,
        "cgroup_controllers": sorted((cgroup_controllers or "").split()),
    }


def build_bubblewrap_command(
    bubblewrap_path: str,
    workspace: Path,
    child_code: str,
    *,
    child_environment: dict[str, str] | None = None,
) -> list[str]:
    if not workspace.is_dir():
        raise IsolationProbeError("workspace must be an existing directory")
    environment = {"PATH": "/usr/bin:/bin"}
    environment.update(child_environment or {})
    if len(environment) > 32:
        raise IsolationProbeError("child environment exceeds 32 entries")

    command = [
        bubblewrap_path,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
    ]
    for runtime_mount in RUNTIME_MOUNTS:
        if Path(runtime_mount).exists():
            command.extend(["--ro-bind", runtime_mount, runtime_mount])
    command.extend(
        [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/run",
            "--bind",
            str(workspace.resolve()),
            "/workspace",
            "--chdir",
            "/workspace",
            "--clearenv",
        ]
    )
    for name, value in sorted(environment.items()):
        if not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise IsolationProbeError(f"invalid child environment name: {name}")
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
            raise IsolationProbeError(f"invalid child environment value: {name}")
        command.extend(["--setenv", name, value])
    command.extend([CHILD_PYTHON, "-c", child_code])
    return command


async def _read_bounded(
    stream: asyncio.StreamReader,
    shared_size: list[int],
    output_limit: int,
) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return b"".join(chunks)
        shared_size[0] += len(chunk)
        if shared_size[0] > output_limit:
            raise OutputLimitExceeded(
                f"child output exceeded {output_limit} bytes"
            )
        chunks.append(chunk)


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def run_owned_process(
    command: list[str],
    *,
    output_limit: int,
    timeout_seconds: float,
) -> tuple[bytes, bytes]:
    if output_limit < 1 or output_limit > MAX_OUTPUT_LIMIT:
        raise IsolationProbeError("output limit is outside the approved range")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise IsolationProbeError("timeout is outside the approved range")

    host_environment = {
        "PATH": "/usr/bin:/bin",
        "ATLAS_FORBIDDEN_CANARY": "synthetic-probe-only",
    }
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=host_environment,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        await _terminate_process_group(process)
        raise IsolationProbeError("child pipes were not created")

    shared_size = [0]
    stdout_task = asyncio.create_task(
        _read_bounded(process.stdout, shared_size, output_limit)
    )
    stderr_task = asyncio.create_task(
        _read_bounded(process.stderr, shared_size, output_limit)
    )
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        stdout, stderr, return_code = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        await _terminate_process_group(process)
        await asyncio.gather(*tasks, return_exceptions=True)
        raise ProcessTimedOut(
            f"child exceeded {timeout_seconds:.3f} seconds"
        ) from error
    except BaseException:
        await _terminate_process_group(process)
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    if return_code != 0:
        error_text = stderr.decode("utf-8", errors="replace")[:512]
        raise IsolationProbeError(
            f"sandbox child exited with {return_code}: {error_text}"
        )
    return stdout, stderr


async def _probe_workspace_and_environment(
    bubblewrap_path: str,
    workspace: Path,
    host_secret: Path,
) -> dict[str, bool]:
    functional_code = f"""
import json
import os
from pathlib import Path
payload = {{
    "workspace_read": Path("/workspace/allowed.txt").read_text() == "allowed",
    "host_hidden": not Path({str(host_secret)!r}).exists(),
    "environment_filtered": "ATLAS_FORBIDDEN_CANARY" not in os.environ,
    "explicit_environment": os.environ.get("ATLAS_PROBE_VISIBLE") == "visible",
}}
Path("/workspace/created.txt").write_text("created")
payload["workspace_write"] = Path("/workspace/created.txt").is_file()
print(json.dumps(payload, sort_keys=True))
"""
    command = build_bubblewrap_command(
        bubblewrap_path,
        workspace,
        functional_code,
        child_environment={"ATLAS_PROBE_VISIBLE": "visible"},
    )
    stdout, _ = await run_owned_process(
        command,
        output_limit=DEFAULT_OUTPUT_LIMIT,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )
    checks = json.loads(stdout)
    if not isinstance(checks, dict) or not all(checks.values()):
        raise IsolationProbeError("workspace/environment isolation check failed")
    return checks


async def _probe_output_limit(bubblewrap_path: str, workspace: Path) -> bool:
    command = build_bubblewrap_command(
        bubblewrap_path,
        workspace,
        "import os, time; os.write(1, b'x' * 65536); time.sleep(5)",
    )
    try:
        await run_owned_process(
            command,
            output_limit=DEFAULT_OUTPUT_LIMIT,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        )
    except OutputLimitExceeded:
        return True
    raise IsolationProbeError("output limit was not enforced")


async def _probe_process_tree_cancellation(
    bubblewrap_path: str,
    workspace: Path,
) -> bool:
    leak_marker = workspace / "descendant-leak.txt"
    cancellation_code = """
import subprocess
import sys
import time
subprocess.Popen([
    sys.executable,
    "-c",
    "import time; from pathlib import Path; "
    "time.sleep(0.8); Path('/workspace/descendant-leak.txt').write_text('leaked')",
])
time.sleep(10)
"""
    command = build_bubblewrap_command(
        bubblewrap_path, workspace, cancellation_code
    )
    try:
        await run_owned_process(
            command,
            output_limit=DEFAULT_OUTPUT_LIMIT,
            timeout_seconds=0.2,
        )
    except ProcessTimedOut:
        await asyncio.sleep(1)
    else:
        raise IsolationProbeError("cancellation probe did not time out")
    if leak_marker.exists():
        raise IsolationProbeError("a descendant survived process cancellation")
    return True


def _probe_missing_isolation(probe_root: Path, workspace: Path) -> bool:
    fail_closed_marker = workspace / "fail-closed.txt"
    try:
        resolve_bubblewrap(str(probe_root / "missing-bwrap"))
    except IsolationUnavailable:
        return not fail_closed_marker.exists()
    raise IsolationProbeError("missing isolation was not denied")


async def run_probe(bubblewrap_path: str) -> dict[str, Any]:
    capabilities = detect_capabilities(bubblewrap_path)
    with tempfile.TemporaryDirectory(prefix="atlas-isolation-probe-") as temporary:
        probe_root = Path(temporary)
        workspace = probe_root / "workspace"
        workspace.mkdir(mode=0o700)
        (workspace / "allowed.txt").write_text("allowed", encoding="utf-8")
        host_secret = probe_root / "host-secret.txt"
        host_secret.write_text("synthetic-secret", encoding="utf-8")

        functional_checks = await _probe_workspace_and_environment(
            bubblewrap_path, workspace, host_secret
        )
        output_limit_enforced = await _probe_output_limit(
            bubblewrap_path, workspace
        )
        process_tree_cancelled = await _probe_process_tree_cancellation(
            bubblewrap_path, workspace
        )
        missing_isolation_denied = _probe_missing_isolation(probe_root, workspace)

    return {
        "status": "pass",
        "capabilities": capabilities,
        "checks": {
            **functional_checks,
            "output_limit_enforced": output_limit_enforced,
            "process_tree_cancelled": process_tree_cancelled,
            "missing_isolation_denied": missing_isolation_denied,
        },
        "not_claimed": [
            "cgroup CPU/memory/PID enforcement",
            "proxy-mediated allowlisted network",
            "macOS containment",
            "Windows containment",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the disposable Atlas Python Linux isolation probe."
    )
    parser.add_argument("--bwrap", help="Explicit bubblewrap executable path.")
    arguments = parser.parse_args()

    try:
        bubblewrap_path = resolve_bubblewrap(arguments.bwrap)
        result = asyncio.run(run_probe(bubblewrap_path))
    except (OSError, subprocess.SubprocessError, IsolationProbeError) as error:
        print(f"Isolation probe failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
