"""Validate the default-deny Atlas Python privileged-operation registry."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = (
    ROOT / "docs" / "harness" / "python-privileged-operation-registry.json"
)

OPERATION_ID_PATTERN = re.compile(r"^harness\.[a-z0-9_.-]{1,120}$")
OPERATION_KINDS = {
    "api_mutation",
    "worker_task",
    "tool",
    "provider_egress",
    "telemetry_egress",
    "persistence",
    "artifact_io",
    "extension_process",
    "parser_process",
    "secret_resolution",
    "subprocess",
}
GATES = {
    "authentication",
    "authorization",
    "schema_validation",
    "idempotency",
    "policy",
    "budget",
    "egress",
    "sandbox",
    "result_filter",
    "audit",
    "cancellation",
}
SIDE_EFFECTS = {"none", "idempotent", "reconcilable", "non_idempotent"}
REGISTRATION_STATES = {"planned_disabled", "registered"}
BASE_GATES = {
    "authorization",
    "schema_validation",
    "policy",
    "budget",
    "audit",
    "cancellation",
}
KIND_GATES = {
    "api_mutation": {"authentication"},
    "worker_task": {"authentication", "sandbox", "result_filter"},
    "tool": {"sandbox", "result_filter"},
    "provider_egress": {"egress", "result_filter"},
    "telemetry_egress": {"egress", "result_filter"},
    "persistence": {"result_filter"},
    "artifact_io": {"result_filter"},
    "extension_process": {"sandbox", "result_filter"},
    "parser_process": {"sandbox", "result_filter"},
    "secret_resolution": {"egress", "result_filter"},
    "subprocess": {"sandbox", "result_filter"},
}
MAX_OPERATIONS = 256

REGISTRY_FIELDS = {
    "schema_version",
    "default_disposition",
    "reviewed_at",
    "required_operation_kinds",
    "gate_catalog",
    "operations",
}
OPERATION_FIELDS = {
    "id",
    "kind",
    "owner",
    "description",
    "side_effect",
    "required_gates",
    "failure_mode",
    "registration_state",
    "implementation",
}


class RegistryError(ValueError):
    """The privileged-operation registry violates a security invariant."""


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryError(f"{field} must be an object")
    return value


def _string(value: Any, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise RegistryError(f"{field} exceeds {maximum} characters")
    return value


def _unique_strings(value: Any, field: str) -> set[str]:
    if not isinstance(value, list) or not value:
        raise RegistryError(f"{field} must be a non-empty list")
    strings = [_string(item, f"{field}[]") for item in value]
    if len(strings) != len(set(strings)):
        raise RegistryError(f"{field} must contain unique values")
    return set(strings)


def _validate_implementation(
    implementation: Any,
    registration_state: str,
    operation_id: str,
    *,
    root: Path,
) -> bool:
    if registration_state == "planned_disabled":
        if implementation is not None:
            raise RegistryError(
                f"{operation_id} is disabled and cannot name an implementation"
            )
        return False

    implementation_path = _string(
        implementation, f"{operation_id}.implementation", maximum=240
    )
    relative_path = PurePosixPath(implementation_path)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or not relative_path.parts
        or relative_path.parts[0] not in {"app", "scripts"}
        or relative_path.suffix != ".py"
    ):
        raise RegistryError(
            f"{operation_id}.implementation must be a repository-relative "
            "Python path under app/ or scripts/"
        )
    if not (root / relative_path).is_file():
        raise RegistryError(f"{operation_id}.implementation does not exist")
    return True


def _validate_operation(
    value: Any,
    index: int,
    *,
    root: Path,
) -> tuple[str, str, bool]:
    operation = _mapping(value, f"operations[{index}]")
    if set(operation) != OPERATION_FIELDS:
        raise RegistryError(f"operations[{index}] has missing or unknown fields")

    operation_id = _string(operation["id"], f"operations[{index}].id", maximum=128)
    if not OPERATION_ID_PATTERN.fullmatch(operation_id):
        raise RegistryError(f"{operation_id} has an invalid operation id")

    operation_kind = _string(operation["kind"], f"{operation_id}.kind")
    if operation_kind not in OPERATION_KINDS:
        raise RegistryError(f"{operation_id} has an invalid kind")
    _string(operation["owner"], f"{operation_id}.owner", maximum=128)
    _string(operation["description"], f"{operation_id}.description")

    side_effect = _string(operation["side_effect"], f"{operation_id}.side_effect")
    if side_effect not in SIDE_EFFECTS:
        raise RegistryError(f"{operation_id} has an invalid side_effect")

    required_gates = _unique_strings(
        operation["required_gates"], f"{operation_id}.required_gates"
    )
    unknown_gates = required_gates - GATES
    if unknown_gates:
        raise RegistryError(f"{operation_id} has unknown gates: {unknown_gates}")
    minimum_gates = BASE_GATES | KIND_GATES[operation_kind]
    if side_effect != "none":
        minimum_gates.add("idempotency")
    missing_gates = minimum_gates - required_gates
    if missing_gates:
        raise RegistryError(f"{operation_id} is missing gates: {missing_gates}")

    if operation["failure_mode"] != "fail_closed":
        raise RegistryError(f"{operation_id} must fail closed")
    registration_state = _string(
        operation["registration_state"], f"{operation_id}.registration_state"
    )
    if registration_state not in REGISTRATION_STATES:
        raise RegistryError(f"{operation_id} has an invalid registration_state")
    is_registered = _validate_implementation(
        operation["implementation"],
        registration_state,
        operation_id,
        root=root,
    )
    return operation_id, operation_kind, is_registered


def validate_registry(payload: Any, *, root: Path = ROOT) -> tuple[int, int]:
    registry = _mapping(payload, "registry")
    if set(registry) != REGISTRY_FIELDS:
        raise RegistryError("registry has missing or unknown fields")
    if registry["schema_version"] != 1:
        raise RegistryError("schema_version must be 1")
    if registry["default_disposition"] != "deny":
        raise RegistryError("default_disposition must be deny")
    try:
        date.fromisoformat(_string(registry["reviewed_at"], "reviewed_at"))
    except ValueError as error:
        raise RegistryError("reviewed_at must be an ISO-8601 date") from error

    required_kinds = _unique_strings(
        registry["required_operation_kinds"], "required_operation_kinds"
    )
    if required_kinds != OPERATION_KINDS:
        raise RegistryError("required_operation_kinds must cover the full catalog")
    gate_catalog = _unique_strings(registry["gate_catalog"], "gate_catalog")
    if gate_catalog != GATES:
        raise RegistryError("gate_catalog must cover the full catalog")

    operations = registry["operations"]
    if not isinstance(operations, list) or not operations:
        raise RegistryError("operations must be a non-empty list")
    if len(operations) > MAX_OPERATIONS:
        raise RegistryError(f"operations exceeds the {MAX_OPERATIONS} entry limit")

    operation_ids: set[str] = set()
    covered_kinds: set[str] = set()
    registered_count = 0
    for index, operation_value in enumerate(operations):
        operation_id, operation_kind, is_registered = _validate_operation(
            operation_value, index, root=root
        )
        if operation_id in operation_ids:
            raise RegistryError(f"duplicate operation id: {operation_id}")
        operation_ids.add(operation_id)
        covered_kinds.add(operation_kind)
        registered_count += int(is_registered)
    missing_kinds = required_kinds - covered_kinds
    if missing_kinds:
        raise RegistryError(f"operations do not cover kinds: {missing_kinds}")
    return len(operation_ids), registered_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Atlas Python privileged-operation invariants."
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    arguments = parser.parse_args()

    try:
        payload = json.loads(arguments.registry.read_text(encoding="utf-8"))
        operation_count, registered_count = validate_registry(payload)
    except (OSError, json.JSONDecodeError, RegistryError) as error:
        print(f"Privileged-operation verification failed: {error}", file=sys.stderr)
        return 1

    print(
        "Privileged-operation registry valid: "
        f"{operation_count} operations, {registered_count} registered"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
