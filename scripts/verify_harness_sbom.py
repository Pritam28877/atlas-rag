#!/usr/bin/env python3
"""Generate a reproducible CycloneDX graph from the uv and npm lockfiles."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import quote


class SbomError(ValueError):
    """Raised when SBOM generation or validation fails."""


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SbomError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise SbomError(f"{label} keys must be strings")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise SbomError(f"{label} must be an array")
    return cast(list[object], value)


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SbomError(f"{label} must be a non-empty string")
    return value


def validate_sbom(document: Mapping[str, object]) -> tuple[int, int]:
    if document.get("bomFormat") != "CycloneDX":
        raise SbomError("SBOM must use CycloneDX")
    if document.get("specVersion") != "1.5":
        raise SbomError("SBOM must use CycloneDX 1.5")
    if document.get("version") != 1:
        raise SbomError("SBOM document version must be 1")

    metadata = _mapping(document.get("metadata"), "metadata")
    root_component = _mapping(metadata.get("component"), "metadata.component")
    if root_component.get("name") != "rag-api":
        raise SbomError("SBOM root component must be rag-api")
    root_reference = _required_string(
        root_component.get("bom-ref"), "metadata.component.bom-ref"
    )

    components = _array(document.get("components"), "components")
    if not components:
        raise SbomError("SBOM components cannot be empty")
    component_references = {root_reference}
    package_urls: set[str] = set()
    for index, raw_component in enumerate(components):
        component = _mapping(raw_component, f"components[{index}]")
        reference = _required_string(
            component.get("bom-ref"), f"components[{index}].bom-ref"
        )
        package_url = _required_string(
            component.get("purl"), f"components[{index}].purl"
        )
        if reference in component_references:
            raise SbomError(f"duplicate SBOM reference: {reference}")
        if package_url in package_urls:
            raise SbomError(f"duplicate package URL: {package_url}")
        if not package_url.startswith(("pkg:pypi/", "pkg:npm/")):
            raise SbomError(f"unsupported SBOM package URL: {package_url}")
        component_references.add(reference)
        package_urls.add(package_url)

    dependencies = _array(document.get("dependencies"), "dependencies")
    dependency_references: set[str] = set()
    for index, raw_dependency in enumerate(dependencies):
        dependency = _mapping(raw_dependency, f"dependencies[{index}]")
        reference = _required_string(
            dependency.get("ref"), f"dependencies[{index}].ref"
        )
        if reference in dependency_references:
            raise SbomError(f"duplicate dependency reference: {reference}")
        if reference not in component_references:
            raise SbomError(f"dependency references unknown component: {reference}")
        dependency_references.add(reference)
        targets = _array(
            dependency.get("dependsOn"),
            f"dependencies[{index}].dependsOn",
        )
        for target in targets:
            if not isinstance(target, str) or target not in component_references:
                raise SbomError(f"{reference} depends on unknown component: {target}")
    if dependency_references != component_references:
        missing = component_references - dependency_references
        raise SbomError(f"SBOM dependency graph omits components: {sorted(missing)}")
    return len(components), len(dependencies)


def _load_json(path: Path) -> dict[str, object]:
    try:
        return _mapping(
            json.loads(path.read_text(encoding="utf-8")),
            str(path),
        )
    except (OSError, json.JSONDecodeError) as error:
        raise SbomError(f"{path} is unreadable: {error}") from error


def _node_package_name(package_path: str) -> str:
    marker = "node_modules/"
    if marker not in package_path:
        raise SbomError(f"invalid npm lock package path: {package_path}")
    return package_path.rsplit(marker, maxsplit=1)[1]


def _node_reference(name: str, version: str) -> str:
    return f"npm:{name}@{version}"


def _augment_node_components(
    document: dict[str, object],
    root: Path,
) -> None:
    lock = _load_json(root / "package-lock.json")
    packages = _mapping(lock.get("packages"), "package-lock packages")
    components = _array(document.get("components"), "components")
    dependencies = _array(document.get("dependencies"), "dependencies")
    path_references: dict[str, str] = {}

    for package_path, raw_package in sorted(packages.items()):
        if not package_path:
            continue
        package = _mapping(raw_package, f"npm package {package_path}")
        name = _node_package_name(package_path)
        version = _required_string(
            package.get("version"),
            f"npm package {package_path}.version",
        )
        license_identifier = _required_string(
            package.get("license"),
            f"npm package {package_path}.license",
        )
        reference = _node_reference(name, version)
        if reference in path_references.values():
            raise SbomError(f"duplicate npm component reference: {reference}")
        path_references[package_path] = reference
        encoded_name = quote(name, safe="/")
        components.append(
            {
                "bom-ref": reference,
                "licenses": [{"license": {"id": license_identifier}}],
                "name": name,
                "purl": f"pkg:npm/{encoded_name}@{version}",
                "type": "library",
                "version": version,
            }
        )

    for package_path, raw_package in sorted(packages.items()):
        if not package_path:
            continue
        package = _mapping(raw_package, f"npm package {package_path}")
        raw_targets = package.get("dependencies", {})
        target_versions = _mapping(
            raw_targets,
            f"npm package {package_path}.dependencies",
        )
        target_references: list[str] = []
        for target_name in sorted(target_versions):
            target_path = f"node_modules/{target_name}"
            target_reference = path_references.get(target_path)
            if target_reference is None:
                raise SbomError(
                    f"npm dependency {target_name} has no locked package"
                )
            target_references.append(target_reference)
        dependencies.append(
            {
                "dependsOn": target_references,
                "ref": path_references[package_path],
            }
        )

    root_package = _mapping(packages.get(""), "package-lock root package")
    root_dependencies = _mapping(
        root_package.get("devDependencies", {}),
        "package-lock root devDependencies",
    )
    root_targets: list[str] = []
    for target_name in sorted(root_dependencies):
        target_reference = path_references.get(f"node_modules/{target_name}")
        if target_reference is None:
            raise SbomError(f"root npm dependency {target_name} is not locked")
        root_targets.append(target_reference)
    metadata = _mapping(document.get("metadata"), "metadata")
    root_component = _mapping(metadata.get("component"), "metadata.component")
    root_reference = _required_string(
        root_component.get("bom-ref"),
        "metadata.component.bom-ref",
    )
    for raw_dependency in dependencies:
        dependency = _mapping(raw_dependency, "dependency")
        if dependency.get("ref") == root_reference:
            depends_on = _array(dependency.get("dependsOn"), "root dependsOn")
            depends_on.extend(root_targets)
            depends_on.sort()
            break
    components.sort(key=lambda component: str(_mapping(component, "component")["purl"]))
    dependencies.sort(
        key=lambda dependency: str(_mapping(dependency, "dependency")["ref"])
    )


def _generate_sbom(root: Path, destination: Path) -> dict[str, object]:
    command = [
        "uv",
        "export",
        "--locked",
        "--all-groups",
        "--format",
        "cyclonedx1.5",
        "--output-file",
        str(destination),
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        output = completed.stderr.strip() or completed.stdout.strip()
        raise SbomError(f"uv SBOM export failed: {output}")
    if destination.stat().st_size > 8 * 1024 * 1024:
        raise SbomError("generated SBOM exceeds 8 MiB")
    document = _load_json(destination)
    _augment_node_components(document, root)
    return document


def _stable_document(document: Mapping[str, object]) -> dict[str, object]:
    stable = copy.deepcopy(dict(document))
    stable.pop("serialNumber", None)
    metadata = _mapping(stable.get("metadata"), "metadata")
    metadata.pop("timestamp", None)
    return stable


def verify_reproducible_sbom(root: Path) -> tuple[int, int]:
    with tempfile.TemporaryDirectory(prefix="atlas-harness-sbom-") as directory:
        temporary_root = Path(directory)
        first = _generate_sbom(root, temporary_root / "first.json")
        second = _generate_sbom(root, temporary_root / "second.json")
    counts = validate_sbom(first)
    if _stable_document(first) != _stable_document(second):
        raise SbomError("normalized SBOM output is not reproducible")
    return counts


def main() -> int:
    try:
        components, dependencies = verify_reproducible_sbom(Path.cwd())
    except (OSError, SbomError, subprocess.TimeoutExpired) as error:
        print(f"SBOM verification failed: {error}", file=sys.stderr)
        return 1
    print(
        f"SBOM valid and reproducible: {components} components, "
        f"{dependencies} dependency records"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
