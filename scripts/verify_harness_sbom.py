#!/usr/bin/env python3
"""Generate and validate a reproducible CycloneDX dependency graph from uv.lock."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast


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
        if not package_url.startswith("pkg:pypi/"):
            raise SbomError(f"non-PyPI component in Python SBOM: {package_url}")
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
    try:
        return _mapping(
            json.loads(destination.read_text(encoding="utf-8")),
            str(destination),
        )
    except (OSError, json.JSONDecodeError) as error:
        raise SbomError(f"generated SBOM is unreadable: {error}") from error


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
