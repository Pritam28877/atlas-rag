import json
import os
import subprocess
import sys
from pathlib import Path

from tests.harness_configured_catalog_fixtures import (
    configured_catalog_fixture,
)

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_harness_credential_broker.py"
HANDLE = "pcr_" + "9" * 32
CANARY = "atlas-secret-canary-must-never-escape"
VARIABLE = "ATLAS_DRILL_PROVIDER_KEY"


def test_real_environment_credential_broker_drill(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        configured_catalog_fixture().configuration.model_dump_json(),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(ROOT),
        VARIABLE: CANARY,
    }

    result = subprocess.run(
        (
            sys.executable,
            str(PROBE),
            "--config-path",
            str(config_path),
            "--handle",
            HANDLE,
            "--provider",
            "configured-provider",
            "--destination-sha256",
            "6" * 64,
            "--environment-variable",
            VARIABLE,
        ),
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert CANARY not in result.stdout
    assert CANARY not in result.stderr
    assert json.loads(result.stdout) == {
        "active_leases": 0,
        "lease_released": True,
        "material_present": True,
        "material_zeroed": True,
        "representation_redacted": True,
    }
