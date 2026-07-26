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
PROBE = ROOT / "scripts" / "probe_harness_provider_routing.py"


def test_real_provider_routing_drill(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    config_path = tmp_path / "providers.json"
    content = (
        configured_catalog_fixture()
        .configuration.model_dump_json()
        .encode()
    )
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
    report = json.loads(result.stdout)
    assert report == {
        "candidate_count": 3,
        "configuration_sha256": hashlib.sha256(content).hexdigest(),
        "decision_sha256": (
            "2e244f110c841bf73da4e8270135072639d316ad5c67e4c0f3aa3cb1c998085a"
        ),
        "eligible_routes": ["configured.primary"],
        "rejected_routes": {
            "configured.secondary": {
                "codes": ["health"],
                "price_version_sha256": "b" * 64,
            },
            "secondary.primary": {
                "codes": ["region"],
                "price_version_sha256": "c" * 64,
            },
        },
        "secret_free": True,
        "selected_route_id": "configured.primary",
        "selection_reason": (
            "Selected configured.primary by health, configured priority, "
            "estimated cost, and route ID; health=healthy; priority=100; "
            "estimated_cost_microusd=2000; "
            f"model_revision_sha256={'3' * 64}; "
            "health_snapshot_sha256="
            "4c90486cb1eaa8a1703e602b7d48cae66198851e3245013cb0ad01ca6376b510; "
            f"price_version_sha256={'7' * 64}."
        ),
    }
