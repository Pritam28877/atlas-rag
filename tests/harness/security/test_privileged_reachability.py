import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from scripts.harness_privileged_reachability import (
    discover_privileged_calls,
)
from scripts.verify_harness_privileged_reachability import (
    ReachabilityError,
    validate_reachability,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    ROOT / "docs/harness/python-privileged-reachability.json"
)
REGISTRY_PATH = (
    ROOT / "docs/harness/python-privileged-operation-registry.json"
)


def load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def load_registry() -> dict[str, object]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_checked_in_reachability_passes_real_cli() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_harness_privileged_reachability.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "Privileged reachability valid: 12 callsites, 0 registered"
    )


def test_synthetic_unclassified_subprocess_fails_gate(
    tmp_path: Path,
) -> None:
    temporary_root = tmp_path / "repository"
    shutil.copytree(ROOT / "app", temporary_root / "app")
    bypass = (
        temporary_root
        / "app/services/harness/synthetic_privileged_bypass.py"
    )
    bypass.write_text(
        "from asyncio import create_subprocess_exec as spawn\n"
        "\n"
        "async def bypass() -> None:\n"
        "    await spawn('/bin/true')\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ReachabilityError,
        match="unclassified privileged callsite.*synthetic_privileged_bypass",
    ):
        validate_reachability(
            load_manifest(),
            load_registry(),
            root=temporary_root,
        )


def test_common_process_network_and_route_bypasses_are_discovered(
    tmp_path: Path,
) -> None:
    service_root = tmp_path / "app/services/harness"
    api_root = tmp_path / "app/api/v1/harness"
    service_root.mkdir(parents=True)
    api_root.mkdir(parents=True)
    (service_root / "bypass.py").write_text(
        "import httpx\n"
        "import subprocess as commands\n"
        "from asyncio import create_subprocess_exec as spawn\n"
        "\n"
        "async def bypass() -> None:\n"
        "    await spawn('/bin/true')\n"
        "    commands.run(['/bin/true'])\n"
        "    httpx.AsyncClient()\n",
        encoding="utf-8",
    )
    (api_root / "bypass.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/bypass')\n"
        "async def bypass() -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )

    calls = discover_privileged_calls(
        (
            PurePosixPath("app/api/v1/harness"),
            PurePosixPath("app/services/harness"),
        ),
        root=tmp_path,
    )

    assert [call.sink for call in calls] == [
        "api.route.POST",
        "network.client",
        "process.create",
        "process.create",
    ]


def test_every_operation_gate_is_enforced_or_explicitly_missing() -> None:
    manifest = copy.deepcopy(load_manifest())
    vertex_callsite = manifest["callsites"][7]
    vertex_callsite["missing_gates"] = []

    with pytest.raises(
        ReachabilityError,
        match="does not account for operation gates",
    ):
        validate_reachability(manifest, load_registry())


def test_gate_evidence_requires_an_existing_symbol() -> None:
    manifest = copy.deepcopy(load_manifest())
    manifest["callsites"][0]["evidence"]["authentication"] = (
        "app/api/v1/harness/policy.py#missing_symbol"
    )

    with pytest.raises(
        ReachabilityError,
        match="gate evidence symbol does not exist",
    ):
        validate_reachability(manifest, load_registry())


def test_manifest_callsites_must_be_deterministically_sorted() -> None:
    manifest = copy.deepcopy(load_manifest())
    manifest["callsites"][0], manifest["callsites"][1] = (
        manifest["callsites"][1],
        manifest["callsites"][0],
    )

    with pytest.raises(
        ReachabilityError,
        match="uniquely sorted",
    ):
        validate_reachability(manifest, load_registry())


def test_scan_roots_cannot_overlap() -> None:
    manifest = copy.deepcopy(load_manifest())
    manifest["scan_roots"] = [
        "app",
        "app/services/harness",
    ]

    with pytest.raises(ReachabilityError, match="must not overlap"):
        validate_reachability(manifest, load_registry())
