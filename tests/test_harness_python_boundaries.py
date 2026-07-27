import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_harness_python_boundaries import (
    MAX_SOURCE_FILES,
    BoundaryError,
    validate_boundaries,
)

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES_PATH = ROOT / "docs" / "harness" / "python-package-boundaries.json"


def load_boundaries() -> dict[str, object]:
    return json.loads(BOUNDARIES_PATH.read_text(encoding="utf-8"))


def copy_package_layout(destination: Path) -> None:
    for relative_path in (
        Path("app/services/harness"),
        Path("app/api/v1/harness"),
        Path("app/cli/harness"),
    ):
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT / relative_path, target)


def test_checked_in_boundaries_pass_real_cli() -> None:
    package_count, file_count, edge_count = validate_boundaries(load_boundaries())
    result = subprocess.run(
        [sys.executable, "scripts/verify_harness_python_boundaries.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "Python harness boundaries valid: "
        f"{package_count} packages, {file_count} files, "
        f"{edge_count} dependency edges"
    )


def test_all_packages_import_without_infrastructure_or_threads() -> None:
    module_names = [
        "app.services.harness",
        *[
            f"app.services.harness.{package['name']}"
            for package in load_boundaries()["packages"]
        ],
        "app.api.v1.harness",
        "app.cli.harness",
    ]
    probe = (
        "import importlib, json, sys, threading;"
        "before=len(threading.enumerate());"
        f"[importlib.import_module(name) for name in {module_names!r}];"
        "forbidden={'boto3','celery','fastapi','httpx','sqlalchemy'};"
        "print(json.dumps({'extra_threads':len(threading.enumerate())-before,"
        "'forbidden':sorted(forbidden & sys.modules.keys())}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"extra_threads": 0, "forbidden": []}


def test_declared_dependency_graph_rejects_cycle() -> None:
    boundaries = load_boundaries()
    packages = copy.deepcopy(boundaries["packages"])
    packages[0]["allowed_harness_dependencies"] = ["events"]
    boundaries["packages"] = packages

    with pytest.raises(BoundaryError, match="dependency cycle"):
        validate_boundaries(boundaries)


def test_source_file_budget_remains_bounded(tmp_path: Path) -> None:
    copy_package_layout(tmp_path)
    protocol_path = tmp_path / "app/services/harness/protocol"
    existing_count = len(list((tmp_path / "app").rglob("*.py")))
    for file_number in range(MAX_SOURCE_FILES - existing_count + 1):
        (protocol_path / f"budget_{file_number}.py").write_text(
            '"""Boundary budget fixture."""\n',
            encoding="utf-8",
        )

    with pytest.raises(BoundaryError, match="source files exceeds"):
        validate_boundaries(load_boundaries(), root=tmp_path)


def test_core_package_cannot_import_infrastructure(tmp_path: Path) -> None:
    copy_package_layout(tmp_path)
    (tmp_path / "app/services/harness/protocol/invalid.py").write_text(
        "import fastapi\n",
        encoding="utf-8",
    )

    with pytest.raises(BoundaryError, match="infrastructure from core"):
        validate_boundaries(load_boundaries(), root=tmp_path)


def test_package_cannot_import_unapproved_harness_dependency(
    tmp_path: Path,
) -> None:
    copy_package_layout(tmp_path)
    (tmp_path / "app/services/harness/runtime/invalid.py").write_text(
        "from app.services.harness import providers\n",
        encoding="utf-8",
    )

    with pytest.raises(BoundaryError, match="runtime cannot depend on providers"):
        validate_boundaries(load_boundaries(), root=tmp_path)


def test_service_cannot_import_client_or_worker(tmp_path: Path) -> None:
    copy_package_layout(tmp_path)
    (tmp_path / "app/services/harness/events/invalid.py").write_text(
        "from app.api import router\n",
        encoding="utf-8",
    )

    with pytest.raises(BoundaryError, match="forbidden client/worker"):
        validate_boundaries(load_boundaries(), root=tmp_path)


def test_client_boundary_can_import_fastapi_and_domain(tmp_path: Path) -> None:
    expected_package_count, expected_file_count, expected_edge_count = (
        validate_boundaries(load_boundaries())
    )
    copy_package_layout(tmp_path)
    (tmp_path / "app/api/v1/harness/client.py").write_text(
        "import fastapi\nfrom app.services.harness import protocol\n",
        encoding="utf-8",
    )

    package_count, file_count, edge_count = validate_boundaries(
        load_boundaries(), root=tmp_path
    )

    assert (package_count, file_count, edge_count) == (
        expected_package_count,
        expected_file_count + 1,
        expected_edge_count,
    )


def test_subprocess_import_is_owned_only_by_sandbox(tmp_path: Path) -> None:
    copy_package_layout(tmp_path)
    (tmp_path / "app/services/harness/tools/invalid.py").write_text(
        "import subprocess\n",
        encoding="utf-8",
    )

    with pytest.raises(BoundaryError, match="outside its owners"):
        validate_boundaries(load_boundaries(), root=tmp_path)


def test_import_time_process_creation_is_rejected(tmp_path: Path) -> None:
    copy_package_layout(tmp_path)
    (tmp_path / "app/services/harness/sandbox/invalid.py").write_text(
        "import subprocess\nsubprocess.run(['true'])\n",
        encoding="utf-8",
    )

    with pytest.raises(BoundaryError, match="import-time side effect"):
        validate_boundaries(load_boundaries(), root=tmp_path)


def test_import_time_process_alias_is_rejected(tmp_path: Path) -> None:
    copy_package_layout(tmp_path)
    (tmp_path / "app/services/harness/sandbox/invalid.py").write_text(
        "from subprocess import run as execute\nexecute(['true'])\n",
        encoding="utf-8",
    )

    with pytest.raises(BoundaryError, match="subprocess.run"):
        validate_boundaries(load_boundaries(), root=tmp_path)
