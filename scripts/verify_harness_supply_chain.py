#!/usr/bin/env python3
"""Verify locked dependency sources, licenses, runtimes, and vulnerability gates."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from datetime import date
from importlib.metadata import Distribution, distributions
from pathlib import Path
from typing import cast
from urllib.parse import urlparse


class SupplyChainError(ValueError):
    """Raised when a supply-chain invariant is violated."""


_NAME_SEPARATOR = re.compile(r"[-_.]+")
_SPDX_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*")
_EXPRESSION_OPERATORS = {"AND", "OR", "WITH"}
_CLASSIFIER_LICENSES = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}
_LEGACY_LICENSE_PREFIXES = {
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "bsd": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "lgpl-3.0-only": "LGPL-3.0-only",
    "mit": "MIT",
    "mit-0": "MIT-0",
    "mit-cmu": "MIT-CMU",
    "mit license": "MIT",
    "mpl-2.0": "MPL-2.0",
    "new bsd": "BSD-3-Clause",
    "psf-2.0": "PSF-2.0",
    "3-clause bsd license": "BSD-3-Clause",
}


def _normalize_name(name: str) -> str:
    return _NAME_SEPARATOR.sub("-", name).lower()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SupplyChainError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise SupplyChainError(f"{label} keys must be strings")
    return cast(dict[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupplyChainError(f"{label} must be a non-empty string")
    return value


def _string_set(value: object, label: str) -> set[str]:
    if not isinstance(value, list) or not value:
        raise SupplyChainError(f"{label} must be a non-empty string array")
    if not all(isinstance(entry, str) and entry for entry in value):
        raise SupplyChainError(f"{label} must contain only non-empty strings")
    return set(cast(list[str], value))


def _load_json(path: Path) -> dict[str, object]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, json.JSONDecodeError) as error:
        raise SupplyChainError(f"cannot read {path}: {error}") from error


def _load_toml(path: Path) -> dict[str, object]:
    try:
        return _mapping(tomllib.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SupplyChainError(f"cannot read {path}: {error}") from error


def validate_license_expression(expression: str, allowed: set[str]) -> None:
    identifiers = {
        token
        for token in _SPDX_IDENTIFIER.findall(expression)
        if token not in _EXPRESSION_OPERATORS
    }
    if not identifiers:
        raise SupplyChainError(f"license expression is empty: {expression!r}")
    forbidden = identifiers - allowed
    if forbidden:
        raise SupplyChainError(
            f"license expression contains forbidden identifiers: {sorted(forbidden)}"
        )


def _classifier_expression(distribution: Distribution) -> str | None:
    expressions: list[str] = []
    classifiers = distribution.metadata.get_all("Classifier", [])
    for classifier in classifiers:
        prefix = "License :: OSI Approved :: "
        if not classifier.startswith(prefix):
            continue
        classifier_name = classifier.removeprefix(prefix)
        expression = _CLASSIFIER_LICENSES.get(classifier_name)
        if expression is not None and expression not in expressions:
            expressions.append(expression)
    if not expressions:
        return None
    return " OR ".join(expressions)


def _legacy_license_expression(distribution: Distribution) -> str | None:
    license_value = distribution.metadata.get("License")
    if license_value:
        normalized = " ".join(license_value.casefold().split())
        for prefix, expression in _LEGACY_LICENSE_PREFIXES.items():
            if normalized == prefix or normalized.startswith(f"{prefix} "):
                return expression
    return _classifier_expression(distribution)


def _license_exception(
    name: str,
    version: str,
    exceptions: Sequence[object],
) -> dict[str, object] | None:
    normalized_name = _normalize_name(name)
    for index, raw_exception in enumerate(exceptions):
        exception = _mapping(raw_exception, f"license_exceptions[{index}]")
        exception_name = _normalize_name(
            _string(exception.get("name"), f"license_exceptions[{index}].name")
        )
        exception_version = _string(
            exception.get("version"), f"license_exceptions[{index}].version"
        )
        if exception_name != normalized_name or exception_version != version:
            continue
        owner = _string(exception.get("owner"), f"{name} exception owner")
        reason = _string(exception.get("reason"), f"{name} exception reason")
        expiry_text = _string(exception.get("expires"), f"{name} exception expiry")
        if len(owner) < 5 or len(reason) < 20:
            raise SupplyChainError(f"{name} license exception lacks ownership detail")
        try:
            expiry = date.fromisoformat(expiry_text)
        except ValueError as error:
            message = f"{name} license exception expiry is invalid"
            raise SupplyChainError(message) from error
        if expiry < date.today():
            raise SupplyChainError(f"{name} license exception expired on {expiry}")
        if exception.get("release_blocking") is not True:
            raise SupplyChainError(f"{name} license exception must block release")
        return exception
    return None


def validate_installed_licenses(policy: Mapping[str, object]) -> tuple[int, int]:
    python_policy = _mapping(policy.get("python"), "python policy")
    allowed = _string_set(
        python_policy.get("allowed_license_identifiers"),
        "allowed_license_identifiers",
    )
    raw_exceptions = python_policy.get("license_exceptions")
    if not isinstance(raw_exceptions, list):
        raise SupplyChainError("license_exceptions must be an array")

    checked = 0
    release_blockers = 0
    seen: set[tuple[str, str]] = set()
    installed = sorted(
        distributions(),
        key=lambda distribution: _normalize_name(distribution.metadata["Name"]),
    )
    for distribution in installed:
        name = distribution.metadata["Name"]
        identity = (_normalize_name(name), distribution.version)
        if identity in seen:
            continue
        seen.add(identity)
        expression = distribution.metadata.get("License-Expression")
        if expression is None:
            expression = _legacy_license_expression(distribution)
        if expression is None:
            exception = _license_exception(name, distribution.version, raw_exceptions)
            if exception is None:
                raise SupplyChainError(
                    f"{name}=={distribution.version} has no verified license expression"
                )
            release_blockers += 1
        else:
            validate_license_expression(expression, allowed)
        checked += 1
    return checked, release_blockers


def validate_python_sources(root: Path, policy: Mapping[str, object]) -> int:
    python_policy = _mapping(policy.get("python"), "python policy")
    project_name = _normalize_name(
        _string(python_policy.get("project_name"), "python project_name")
    )
    allowed_registries = _string_set(
        python_policy.get("allowed_registry_urls"),
        "allowed_registry_urls",
    )
    lock = _load_toml(root / "uv.lock")
    packages = lock.get("package")
    if not isinstance(packages, list) or not packages:
        raise SupplyChainError("uv.lock must contain packages")

    for index, raw_package in enumerate(packages):
        package = _mapping(raw_package, f"uv.lock package[{index}]")
        name = _normalize_name(_string(package.get("name"), "locked package name"))
        _string(package.get("version"), f"{name} locked version")
        source = _mapping(package.get("source"), f"{name} locked source")
        if name == project_name and source == {"editable": "."}:
            continue
        registry = source.get("registry")
        if not isinstance(registry, str) or registry not in allowed_registries:
            raise SupplyChainError(f"{name} uses forbidden or unpinned source {source}")

    project = _load_toml(root / "pyproject.toml")
    tool = _mapping(project.get("tool", {}), "pyproject tool")
    uv_config = _mapping(tool.get("uv", {}), "tool.uv")
    if "sources" in uv_config:
        raise SupplyChainError("tool.uv.sources is forbidden without policy approval")
    return len(packages)


def validate_node_sources(root: Path, policy: Mapping[str, object]) -> int:
    node_policy = _mapping(policy.get("node"), "node policy")
    expected_node = _string(node_policy.get("version"), "node version")
    expected_npm = _string(node_policy.get("npm_version"), "npm version")
    allowed_hosts = _string_set(
        node_policy.get("allowed_registry_hosts"), "allowed_registry_hosts"
    )
    allowed_licenses = _string_set(
        node_policy.get("allowed_license_identifiers"),
        "allowed_license_identifiers",
    )
    actual_node = (root / ".node-version").read_text(encoding="utf-8").strip()
    if actual_node != expected_node:
        raise SupplyChainError(f".node-version must be {expected_node}")

    package = _load_json(root / "package.json")
    engines = _mapping(package.get("engines"), "package engines")
    if engines.get("node") != expected_node or engines.get("npm") != expected_npm:
        raise SupplyChainError("package engines must exactly match the runtime policy")
    if package.get("packageManager") != f"npm@{expected_npm}":
        raise SupplyChainError("packageManager must pin the approved npm version")
    if package.get("private") is not True:
        raise SupplyChainError("generated-contract tooling package must remain private")

    lock = _load_json(root / "package-lock.json")
    if lock.get("lockfileVersion") != 3:
        raise SupplyChainError("package-lock.json must use lockfileVersion 3")
    packages = _mapping(lock.get("packages"), "locked Node packages")
    for package_path, raw_locked_package in packages.items():
        if package_path == "":
            continue
        locked_package = _mapping(raw_locked_package, f"Node package {package_path}")
        resolved = _string(locked_package.get("resolved"), f"{package_path}.resolved")
        integrity = _string(
            locked_package.get("integrity"), f"{package_path}.integrity"
        )
        if urlparse(resolved).hostname not in allowed_hosts:
            raise SupplyChainError(f"{package_path} uses forbidden source {resolved}")
        if not integrity.startswith("sha512-"):
            raise SupplyChainError(f"{package_path} must have sha512 integrity")
        if locked_package.get("link") is True:
            raise SupplyChainError(f"{package_path} cannot be a linked dependency")
        license_expression = _string(
            locked_package.get("license"),
            f"{package_path}.license",
        )
        validate_license_expression(license_expression, allowed_licenses)
    return len(packages) - 1


def run_vulnerability_audits(root: Path) -> None:
    commands = [
        ["uv", "audit", "--locked"],
        [
            "npm",
            "audit",
            "--audit-level=high",
            "--ignore-scripts",
            "--package-lock-only",
        ],
    ]
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            output = completed.stdout.strip() or completed.stderr.strip()
            raise SupplyChainError(
                f"vulnerability audit failed for {command[0]}: {output}"
            )


def validate_release_metadata(root: Path, policy: Mapping[str, object]) -> None:
    artifacts = _mapping(policy.get("artifacts"), "artifact policy")
    for field in ("wheel_max_bytes", "sdist_max_bytes", "container_max_bytes"):
        value = artifacts.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SupplyChainError(f"{field} must be a positive integer")
    if artifacts.get("container_gate_activates_with_parent") != "P15":
        raise SupplyChainError("container size enforcement must activate with P15")

    provenance = _load_json(root / "docs" / "harness" / "provenance-manifest.json")
    outbound = _mapping(provenance.get("outbound_license"), "outbound license")
    notice = (root / "docs" / "harness" / "NOTICE.md").read_text(encoding="utf-8")
    if outbound.get("status") == "pending_human_approval":
        if "pending_human_approval" not in notice:
            raise SupplyChainError("draft NOTICE omits pending license approval")
        if outbound.get("approval_required") is not True:
            raise SupplyChainError("pending outbound license must require approval")
    elif not (root / "LICENSE").is_file():
        raise SupplyChainError("approved outbound license requires a root LICENSE")


def validate_supply_chain(root: Path, *, run_audits: bool) -> tuple[int, int, int, int]:
    policy = _load_json(root / "docs" / "harness" / "supply-chain-policy.json")
    if policy.get("schema_version") != 1:
        raise SupplyChainError("supply-chain policy schema_version must be 1")
    validate_release_metadata(root, policy)
    python_packages = validate_python_sources(root, policy)
    node_packages = validate_node_sources(root, policy)
    licensed_packages, release_blockers = validate_installed_licenses(policy)
    if run_audits:
        run_vulnerability_audits(root)
    return python_packages, node_packages, licensed_packages, release_blockers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--skip-audits", action="store_true")
    arguments = parser.parse_args()
    try:
        counts = validate_supply_chain(
            arguments.root.resolve(),
            run_audits=not arguments.skip_audits,
        )
    except (OSError, SupplyChainError, subprocess.TimeoutExpired) as error:
        print(f"Supply-chain verification failed: {error}", file=sys.stderr)
        return 1
    python_count, node_count, licensed_count, release_blockers = counts
    print(
        "Supply chain valid: "
        f"{python_count} locked Python packages, "
        f"{node_count} locked Node packages, "
        f"{licensed_count} installed licenses, "
        f"{release_blockers} release blockers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
