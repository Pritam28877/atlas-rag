"""Enforce Atlas Harness Python package and import boundaries."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BOUNDARIES = ROOT / "docs" / "harness" / "python-package-boundaries.json"
PACKAGE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
LAYERS = {"domain", "core", "adapter"}
MAX_PACKAGES = 64
MAX_SOURCE_FILES = 256

BOUNDARY_FIELDS = {
    "schema_version",
    "service_root",
    "client_packages",
    "max_python_file_lines",
    "forbidden_service_imports",
    "forbidden_core_imports",
    "side_effect_import_owners",
    "packages",
}
PACKAGE_FIELDS = {"name", "layer", "allowed_harness_dependencies"}
RISKY_MODULE_CALLS = {
    "asyncio.create_task",
    "asyncio.ensure_future",
    "asyncio.run",
    "boto3.client",
    "boto3.resource",
    "celery.Celery",
    "fastapi.APIRouter",
    "httpx.AsyncClient",
    "httpx.Client",
    "os.popen",
    "os.system",
    "socket.create_connection",
    "socket.socket",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.run",
    "threading.Thread",
}
RISKY_CALL_NAMES = {"APIRouter", "Celery"}


class BoundaryError(ValueError):
    """A Python package or import boundary was violated."""


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BoundaryError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoundaryError(f"{field} must be a non-empty string")
    return value


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise BoundaryError(f"{field} must be {qualifier}")
    values = [_string(item, f"{field}[]") for item in value]
    if len(values) != len(set(values)):
        raise BoundaryError(f"{field} must contain unique values")
    return values


def _repository_path(value: Any, field: str) -> PurePosixPath:
    path = PurePosixPath(_string(value, field))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise BoundaryError(f"{field} must stay inside the repository")
    return path


def _matches_import(module_name: str, prefix: str) -> bool:
    return module_name == prefix or module_name.startswith(f"{prefix}.")


def _call_name(call: ast.Call) -> str | None:
    parts: list[str] = []
    current: ast.expr = call.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _top_level_risky_call(tree: ast.Module) -> str | None:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", maxsplit=1)[0]] = (
                    alias.name
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = (
                    f"{node.module}.{alias.name}"
                )

    for statement in tree.body:
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node)
            if call_name is None:
                continue
            first_part, separator, remainder = call_name.partition(".")
            resolved_name = aliases.get(first_part, first_part)
            if separator:
                resolved_name = f"{resolved_name}.{remainder}"
            if (
                resolved_name in RISKY_MODULE_CALLS
                or call_name in RISKY_CALL_NAMES
            ):
                return resolved_name
    return None


def _imports(tree: ast.Module) -> list[tuple[str, int, tuple[str, ...]]]:
    imports: list[tuple[str, int, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, 0, ()))
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                (
                    node.module or "",
                    node.level,
                    tuple(alias.name for alias in node.names),
                )
            )
    return imports


def _harness_dependency(
    module_name: str,
    level: int,
    imported_names: tuple[str, ...],
    current_package: str | None,
) -> set[str]:
    if level > 2:
        raise BoundaryError("relative import escapes the harness package")
    if level == 2:
        if not module_name:
            return set(imported_names)
        return {module_name.split(".", maxsplit=1)[0]}
    if level == 1:
        return set()

    prefix = "app.services.harness"
    if module_name == prefix:
        return set(imported_names)
    if not module_name.startswith(f"{prefix}."):
        return set()
    dependency = module_name.split(".")[3]
    if current_package == dependency:
        return set()
    return {dependency}


def _verify_acyclic(dependencies: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(package: str) -> None:
        if package in visiting:
            raise BoundaryError(f"declared dependency cycle contains {package}")
        if package in visited:
            return
        visiting.add(package)
        for dependency in dependencies[package]:
            visit(dependency)
        visiting.remove(package)
        visited.add(package)

    for package_name in dependencies:
        visit(package_name)


def _parse_packages(
    value: Any,
) -> tuple[dict[str, str], dict[str, set[str]]]:
    if not isinstance(value, list) or not value or len(value) > MAX_PACKAGES:
        raise BoundaryError(f"packages must contain 1 to {MAX_PACKAGES} entries")
    layers: dict[str, str] = {}
    dependencies: dict[str, set[str]] = {}
    for index, package_value in enumerate(value):
        package = _mapping(package_value, f"packages[{index}]")
        if set(package) != PACKAGE_FIELDS:
            raise BoundaryError(f"packages[{index}] has missing or unknown fields")
        name = _string(package["name"], f"packages[{index}].name")
        if not PACKAGE_NAME_PATTERN.fullmatch(name) or name in layers:
            raise BoundaryError(f"invalid or duplicate package name: {name}")
        layer = _string(package["layer"], f"{name}.layer")
        if layer not in LAYERS:
            raise BoundaryError(f"{name} has an invalid layer")
        layers[name] = layer
        dependencies[name] = set(
            _string_list(
                package["allowed_harness_dependencies"],
                f"{name}.allowed_harness_dependencies",
                allow_empty=True,
            )
        )
    for name, allowed in dependencies.items():
        unknown = allowed - layers.keys()
        if name in allowed or unknown:
            raise BoundaryError(f"{name} has invalid dependencies: {unknown | {name}}")
    _verify_acyclic(dependencies)
    return layers, dependencies


def _scan_file(
    path: Path,
    *,
    current_package: str | None,
    is_client: bool,
    layers: dict[str, str],
    allowed_dependencies: dict[str, set[str]],
    forbidden_service_imports: list[str],
    forbidden_core_imports: list[str],
    side_effect_owners: dict[str, set[str]],
    max_lines: int,
) -> set[tuple[str, str]]:
    source = path.read_text(encoding="utf-8")
    if len(source.splitlines()) > max_lines:
        raise BoundaryError(f"{path} exceeds the {max_lines}-line limit")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        raise BoundaryError(f"{path} is not valid Python: {error}") from error

    risky_call = _top_level_risky_call(tree)
    if risky_call:
        raise BoundaryError(f"{path} performs import-time side effect: {risky_call}")

    edges: set[tuple[str, str]] = set()
    for module_name, level, imported_names in _imports(tree):
        if not is_client and any(
            _matches_import(module_name, prefix)
            for prefix in forbidden_service_imports
        ):
            raise BoundaryError(f"{path} imports forbidden client/worker module")
        if current_package and layers[current_package] in {"domain", "core"}:
            if any(
                _matches_import(module_name, prefix)
                for prefix in forbidden_core_imports
            ):
                raise BoundaryError(f"{path} imports infrastructure from core")
        for side_effect_import, owners in side_effect_owners.items():
            if _matches_import(module_name, side_effect_import):
                if not is_client and current_package not in owners:
                    raise BoundaryError(
                        f"{path} imports {side_effect_import} outside its owners"
                    )
        dependencies = _harness_dependency(
            module_name, level, imported_names, current_package
        )
        if current_package is None and not is_client and dependencies:
            raise BoundaryError("harness package root must remain inert")
        for dependency in dependencies:
            if dependency not in layers:
                raise BoundaryError(f"{path} imports unknown harness package")
            if is_client:
                continue
            if dependency not in allowed_dependencies[current_package]:
                raise BoundaryError(
                    f"{current_package} cannot depend on {dependency}"
                )
            edges.add((current_package, dependency))
    return edges


def validate_boundaries(payload: Any, *, root: Path = ROOT) -> tuple[int, int, int]:
    boundaries = _mapping(payload, "boundaries")
    if set(boundaries) != BOUNDARY_FIELDS:
        raise BoundaryError("boundaries has missing or unknown fields")
    if boundaries["schema_version"] != 1:
        raise BoundaryError("schema_version must be 1")
    service_root = _repository_path(boundaries["service_root"], "service_root")
    client_packages = [
        _repository_path(value, "client_packages[]")
        for value in _string_list(boundaries["client_packages"], "client_packages")
    ]
    max_lines = boundaries["max_python_file_lines"]
    if not isinstance(max_lines, int) or not 1 <= max_lines <= 500:
        raise BoundaryError("max_python_file_lines must be between 1 and 500")

    layers, allowed_dependencies = _parse_packages(boundaries["packages"])
    forbidden_service_imports = _string_list(
        boundaries["forbidden_service_imports"], "forbidden_service_imports"
    )
    forbidden_core_imports = _string_list(
        boundaries["forbidden_core_imports"], "forbidden_core_imports"
    )
    raw_owners = _mapping(
        boundaries["side_effect_import_owners"], "side_effect_import_owners"
    )
    side_effect_owners = {
        module_name: set(
            _string_list(
                owners,
                f"side_effect_import_owners.{module_name}",
                allow_empty=True,
            )
        )
        for module_name, owners in raw_owners.items()
    }
    for module_name, owners in side_effect_owners.items():
        if not module_name or owners - layers.keys():
            raise BoundaryError(f"invalid side-effect import owners for {module_name}")

    service_path = root / service_root
    if not (service_path / "__init__.py").is_file():
        raise BoundaryError("service_root must be an importable package")
    for package_name in layers:
        if not (service_path / package_name / "__init__.py").is_file():
            raise BoundaryError(f"missing harness package: {package_name}")
    for client_package in client_packages:
        if not (root / client_package / "__init__.py").is_file():
            raise BoundaryError(f"missing client package: {client_package}")

    source_files = sorted(service_path.rglob("*.py"))
    for client_package in client_packages:
        source_files.extend(sorted((root / client_package).rglob("*.py")))
    if len(source_files) > MAX_SOURCE_FILES:
        raise BoundaryError(f"source files exceeds the {MAX_SOURCE_FILES} limit")

    edges: set[tuple[str, str]] = set()
    for source_file in source_files:
        try:
            relative = source_file.relative_to(service_path)
        except ValueError:
            current_package = None
            is_client = True
        else:
            current_package = relative.parts[0] if len(relative.parts) > 1 else None
            is_client = False
        edges.update(
            _scan_file(
                source_file,
                current_package=current_package,
                is_client=is_client,
                layers=layers,
                allowed_dependencies=allowed_dependencies,
                forbidden_service_imports=forbidden_service_imports,
                forbidden_core_imports=forbidden_core_imports,
                side_effect_owners=side_effect_owners,
                max_lines=max_lines,
            )
        )
    package_count = len(layers) + len(client_packages)
    return package_count, len(source_files), len(edges)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Atlas Harness Python package boundaries."
    )
    parser.add_argument("--boundaries", type=Path, default=DEFAULT_BOUNDARIES)
    arguments = parser.parse_args()
    try:
        payload = json.loads(arguments.boundaries.read_text(encoding="utf-8"))
        package_count, file_count, edge_count = validate_boundaries(payload)
    except (OSError, json.JSONDecodeError, BoundaryError) as error:
        print(f"Python boundary verification failed: {error}", file=sys.stderr)
        return 1
    print(
        "Python harness boundaries valid: "
        f"{package_count} packages, {file_count} files, "
        f"{edge_count} dependency edges"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
