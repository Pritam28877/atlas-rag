"""Verify Atlas workspace boundaries, file limits, and release binary budgets."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = ROOT / "atlas-harness"
DEFAULT_LIMITS = DEFAULT_WORKSPACE / "quality-limits.json"


class WorkspaceError(ValueError):
    """The Atlas workspace violates an architectural quality gate."""


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as source_file:
        return json.load(source_file)


def _cargo_metadata(workspace: Path) -> dict[str, Any]:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise WorkspaceError("cargo is unavailable")
    result = subprocess.run(
        [cargo, "metadata", "--locked", "--format-version", "1"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WorkspaceError(f"cargo metadata failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def validate_package_graph(metadata: dict[str, Any], limits: dict[str, Any]) -> None:
    packages = {
        package["id"]: package
        for package in metadata["packages"]
        if package["id"] in metadata["workspace_members"]
    }
    package_names = {package["name"] for package in packages.values()}
    required_packages = set(limits["required_packages"])
    missing_packages = sorted(required_packages - package_names)
    if missing_packages:
        raise WorkspaceError(
            "required workspace packages are missing: " + ", ".join(missing_packages)
        )

    trusted_packages = set(limits["trusted_kernel_packages"])
    client_packages = set(limits["client_packages"])
    id_to_name = {
        package_id: package["name"] for package_id, package in packages.items()
    }
    resolve = metadata.get("resolve")
    if resolve is None:
        raise WorkspaceError("cargo metadata did not include a dependency graph")

    forbidden_edges: list[str] = []
    for node in resolve["nodes"]:
        source_name = id_to_name.get(node["id"])
        if source_name not in trusted_packages:
            continue
        for dependency_id in node["dependencies"]:
            dependency_name = id_to_name.get(dependency_id)
            if dependency_name in client_packages:
                forbidden_edges.append(f"{source_name} -> {dependency_name}")
    if forbidden_edges:
        raise WorkspaceError(
            "trusted-kernel packages depend on clients: "
            + ", ".join(sorted(forbidden_edges))
        )

    for package in packages.values():
        if package["publish"] != []:
            raise WorkspaceError(f"{package['name']} must remain publish=false")


def validate_source_file_limits(workspace: Path, limits: dict[str, Any]) -> None:
    maximum_lines = limits["maximum_rust_file_lines"]
    oversized_files: list[str] = []
    for source_path in sorted((workspace / "crates").rglob("*.rs")):
        line_count = len(source_path.read_text(encoding="utf-8").splitlines())
        if line_count > maximum_lines:
            relative_path = source_path.relative_to(workspace)
            oversized_files.append(f"{relative_path} ({line_count} lines)")
    if oversized_files:
        raise WorkspaceError(
            "Rust source files exceed the hard line limit: "
            + ", ".join(oversized_files)
        )


def validate_release_binaries(workspace: Path, limits: dict[str, Any]) -> None:
    target_directory = workspace / "target" / "release"
    for binary_name, maximum_bytes in limits[
        "maximum_release_binary_bytes"
    ].items():
        binary_path = target_directory / binary_name
        if not binary_path.is_file():
            raise WorkspaceError(f"release binary is missing: {binary_name}")
        observed_bytes = binary_path.stat().st_size
        if observed_bytes > maximum_bytes:
            raise WorkspaceError(
                f"{binary_name} is {observed_bytes} bytes; maximum is {maximum_bytes}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Atlas workspace architecture and quality budgets."
    )
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--limits", type=Path, default=DEFAULT_LIMITS)
    parser.add_argument("--check-release-binaries", action="store_true")
    arguments = parser.parse_args()

    try:
        limits = _load_json(arguments.limits)
        if limits.get("schema_version") != 1:
            raise WorkspaceError("quality limit schema_version must be 1")
        metadata = _cargo_metadata(arguments.workspace)
        validate_package_graph(metadata, limits)
        validate_source_file_limits(arguments.workspace, limits)
        if arguments.check_release_binaries:
            validate_release_binaries(arguments.workspace, limits)
    except (OSError, json.JSONDecodeError, WorkspaceError) as error:
        print(f"Atlas workspace verification failed: {error}", file=sys.stderr)
        return 1

    release_binary_status = (
        "checked" if arguments.check_release_binaries else "skipped"
    )
    print(
        "Atlas workspace valid: "
        f"{len(metadata['workspace_members'])} packages, "
        f"release_binaries={release_binary_status}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
