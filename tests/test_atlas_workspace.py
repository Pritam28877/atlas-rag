import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_atlas_workspace_passes_real_architecture_verifier() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_atlas_workspace.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "Atlas workspace valid: 16 packages, release_binaries=skipped"
    )
