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
PROBE = ROOT / "scripts" / "probe_harness_provider_egress.py"
DESTINATION_URL = "https://api.provider.example/v1"
TARGET_URL = f"{DESTINATION_URL}/responses"
VARIABLE = "ATLAS_DRILL_PROVIDER_KEY"
SECRET_CANARY = "atlas-egress-secret-must-never-persist"
PROMPT_CANARY = "atlas-egress-prompt-must-never-persist"


def test_real_provider_egress_drill(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    destination_sha256 = provider_destination_sha256(DESTINATION_URL)
    configuration = _configuration_for_destination(destination_sha256)
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        configuration.model_dump_json(),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    database_path = tmp_path / "provider-audit.sqlite3"
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(ROOT),
        VARIABLE: SECRET_CANARY,
    }

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
        env=environment,
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
        "audit_event_types": [
            "Provider.AttemptStarted",
            "Provider.AttemptCompleted",
        ],
        "audit_events": 2,
        "authorization_transmitted": True,
        "dlp_blocked": True,
        "journal_secret_free": True,
        "network_calls": [["1.1.1.1", 443, None]],
        "private_address_blocked": True,
        "response_status": 200,
        "temporary_auth_zeroed": True,
        "tls_server_hostname": "api.provider.example",
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
