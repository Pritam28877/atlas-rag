import json
import os
import subprocess
import sys
from pathlib import Path

from app.services.harness.providers import (
    ProviderConfiguration,
    provider_destination_sha256,
)
from tests.harness_configured_catalog_fixtures import (
    configured_catalog_fixture,
)

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_harness_provider_dispatch.py"
DESTINATION_URL = "https://api.provider.example/v1"
TARGET_URL = f"{DESTINATION_URL}/responses"
VARIABLE = "ATLAS_DISPATCH_DRILL_PROVIDER_KEY"
SECRET_CANARY = "atlas-dispatch-secret-must-never-persist"
PROMPT_CANARY = "atlas-dispatch-prompt-must-never-persist"
RETRY_REQUEST_ID = "req_" + "3" * 32
AMBIGUOUS_REQUEST_ID = "req_" + "4" * 32
CAPPED_REQUEST_ID = "req_" + "5" * 32


def test_provider_dispatch_restart_and_budget_drill(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    configuration = _configuration_for_destination(
        provider_destination_sha256(DESTINATION_URL)
    )
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        configuration.model_dump_json(),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    database_path = tmp_path / "provider-dispatch.sqlite3"

    result = subprocess.run(
        (
            sys.executable,
            str(PROBE),
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--destination-url",
            DESTINATION_URL,
            "--target-url",
            TARGET_URL,
            "--environment-variable",
            VARIABLE,
            "--prompt-canary",
            PROMPT_CANARY,
        ),
        cwd=ROOT,
        env={
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT),
            VARIABLE: SECRET_CANARY,
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert SECRET_CANARY not in result.stdout
    assert SECRET_CANARY not in result.stderr
    assert PROMPT_CANARY not in result.stdout
    assert PROMPT_CANARY not in result.stderr
    assert json.loads(result.stdout) == {
        "active_leases": 0,
        "ambiguous_error": "ambiguous",
        "audit_events": 8,
        "call_counts": {
            AMBIGUOUS_REQUEST_ID: 1,
            CAPPED_REQUEST_ID: 1,
            RETRY_REQUEST_ID: 2,
        },
        "capped_error": "cost",
        "costs_after_restart": {
            "ambiguous": 50,
            "capped": 50,
            "retry": 80,
        },
        "credential_present": True,
        "database_secret_free": True,
        "delays_ms": [0, 0],
        "replay_error": "cost",
        "replay_redispatched": False,
        "response_status": 200,
    }


def _configuration_for_destination(
    destination_sha256: str,
) -> ProviderConfiguration:
    values = (
        configured_catalog_fixture()
        .configuration.model_dump(mode="python")
    )
    credentials = []
    for binding in values["credential_bindings"]:
        if binding["provider"] == "configured-provider":
            binding["destination_sha256"] = destination_sha256
        credentials.append(binding)
    policies = []
    for policy in values["data_policies"]:
        if policy["provider"] == "configured-provider":
            policy["destination_sha256"] = destination_sha256
        policies.append(policy)
    values["credential_bindings"] = tuple(credentials)
    values["data_policies"] = tuple(policies)
    return ProviderConfiguration.model_validate(values)
