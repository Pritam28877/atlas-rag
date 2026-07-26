import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_harness_supply_chain import (
    SupplyChainError,
    run_vulnerability_audits,
    validate_license_expression,
    validate_python_sources,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "docs" / "harness" / "supply-chain-policy.json"


def load_policy() -> dict[str, object]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def test_checked_in_supply_chain_passes_real_cli_without_network_audits() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_harness_supply_chain.py",
            "--skip-audits",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Supply chain valid:")


def test_forbidden_license_identifier_is_rejected() -> None:
    allowed = set(load_policy()["python"]["allowed_license_identifiers"])

    with pytest.raises(SupplyChainError, match="forbidden identifiers"):
        validate_license_expression("AGPL-3.0-only", allowed)


def test_unpinned_python_source_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(
        """
version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "untrusted"
version = "1.0.0"
source = { git = "https://example.invalid/repository#main" }
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "fixture"
version = "0.0.0"
requires-python = ">=3.12"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    policy = {
        "python": {
            "project_name": "fixture",
            "allowed_registry_urls": ["https://pypi.org/simple"],
        }
    }

    with pytest.raises(SupplyChainError, match="forbidden or unpinned source"):
        validate_python_sources(tmp_path, policy)


def test_vulnerability_audit_failure_stops_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_audit(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="known vulnerability",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fail_audit)

    with pytest.raises(SupplyChainError, match="known vulnerability"):
        run_vulnerability_audits(ROOT)


def test_supply_chain_policy_records_release_blockers() -> None:
    exceptions = load_policy()["python"]["license_exceptions"]

    assert exceptions
    assert all(exception["release_blocking"] is True for exception in exceptions)
