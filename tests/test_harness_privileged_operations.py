import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.verify_harness_privileged_operations import (
    RegistryError,
    validate_registry,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = (
    ROOT / "docs" / "harness" / "python-privileged-operation-registry.json"
)
SCHEMA_PATH = (
    ROOT
    / "docs"
    / "harness"
    / "python-privileged-operation-registry.schema.json"
)


def load_registry() -> dict[str, object]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_checked_in_registry_passes_real_cli() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_harness_privileged_operations.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "Privileged-operation registry valid: 19 operations, 0 registered"
    )


def test_checked_in_registry_matches_json_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    validator.validate(load_registry())


def test_registry_requires_default_deny() -> None:
    registry = load_registry()
    registry["default_disposition"] = "allow"

    with pytest.raises(RegistryError, match="must be deny"):
        validate_registry(registry)


def test_registry_requires_every_operation_kind() -> None:
    registry = load_registry()
    registry["operations"] = [
        operation
        for operation in registry["operations"]
        if operation["kind"] != "secret_resolution"
    ]

    with pytest.raises(RegistryError, match="do not cover kinds"):
        validate_registry(registry)


def test_side_effect_requires_idempotency_gate() -> None:
    registry = load_registry()
    operation = copy.deepcopy(registry["operations"][0])
    operation["required_gates"].remove("idempotency")
    registry["operations"][0] = operation

    with pytest.raises(RegistryError, match="missing gates"):
        validate_registry(registry)


def test_disabled_operation_cannot_name_implementation() -> None:
    registry = load_registry()
    operation = copy.deepcopy(registry["operations"][0])
    operation["implementation"] = "app/main.py"
    registry["operations"][0] = operation

    with pytest.raises(RegistryError, match="disabled"):
        validate_registry(registry)


def test_registered_operation_requires_existing_scoped_python_file() -> None:
    registry = load_registry()
    operation = copy.deepcopy(registry["operations"][0])
    operation["registration_state"] = "registered"
    operation["implementation"] = "../outside.py"
    registry["operations"][0] = operation

    with pytest.raises(RegistryError, match="under app/ or scripts/"):
        validate_registry(registry)


def test_duplicate_operation_id_is_rejected() -> None:
    registry = load_registry()
    registry["operations"].append(copy.deepcopy(registry["operations"][0]))

    with pytest.raises(RegistryError, match="duplicate operation id"):
        validate_registry(registry)
