import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.probe_harness_python_isolation import (
    IsolationProbeError,
    IsolationUnavailable,
    build_bubblewrap_command,
    resolve_bubblewrap,
)

ROOT = Path(__file__).resolve().parents[1]


def test_missing_bubblewrap_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(IsolationUnavailable, match="not an executable"):
        resolve_bubblewrap(str(tmp_path / "missing-bwrap"))


def test_command_clears_environment_and_mounts_only_workspace(
    tmp_path: Path,
) -> None:
    command = build_bubblewrap_command(
        "/usr/bin/bwrap",
        tmp_path,
        "print('ok')",
        child_environment={"ATLAS_VISIBLE": "yes"},
    )

    assert "--unshare-all" in command
    assert "--die-with-parent" in command
    assert "--clearenv" in command
    assert ["--bind", str(tmp_path.resolve()), "/workspace"] == command[
        command.index("--bind") : command.index("--bind") + 3
    ]
    assert "ATLAS_VISIBLE" in command
    assert "OPENAI_API_KEY" not in command


def test_command_rejects_unbounded_or_invalid_environment(tmp_path: Path) -> None:
    with pytest.raises(IsolationProbeError, match="invalid child environment name"):
        build_bubblewrap_command(
            "/usr/bin/bwrap",
            tmp_path,
            "print('never')",
            child_environment={"bad-name": "value"},
        )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is unavailable")
def test_real_disposable_linux_isolation_probe() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/probe_harness_python_isolation.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert payload["checks"] == {
        "environment_filtered": True,
        "explicit_environment": True,
        "host_hidden": True,
        "missing_isolation_denied": True,
        "output_limit_enforced": True,
        "process_tree_cancelled": True,
        "workspace_read": True,
        "workspace_write": True,
    }
