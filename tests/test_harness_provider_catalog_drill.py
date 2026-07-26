import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.harness_configured_catalog_fixtures import (
    configured_catalog_fixture,
)

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_harness_provider_catalog.py"


def test_real_config_catalog_and_health_drill(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    config_path = tmp_path / "providers.json"
    content = configured_catalog_fixture().configuration.model_dump_json().encode()
    config_path.write_bytes(content)
    os.chmod(config_path, 0o600)

    result = subprocess.run(
        (
            sys.executable,
            str(PROBE),
            "--config-path",
            str(config_path),
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "catalog_pages_secret_free": True,
        "configuration_sha256": hashlib.sha256(content).hexdigest(),
        "health": {
            "configured_routes": 3,
            "lineage_applied": True,
            "status": "healthy",
        },
        "model_pages": {
            "configured-provider": {
                "first_page_count": 2,
                "has_more": False,
                "snapshot_matches": True,
            },
            "secondary-provider": {
                "first_page_count": 1,
                "has_more": False,
                "snapshot_matches": True,
            },
        },
        "provider_page": {
            "has_more": False,
            "providers": [
                "configured-provider",
                "secondary-provider",
            ],
        },
    }
