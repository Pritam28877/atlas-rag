import copy
import json
import subprocess
import sys
from pathlib import Path

from scripts.verify_codex_rpc_inventory import (
    InventoryError,
    validate_inventory,
    validate_registration,
)

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = (
    ROOT / "docs" / "harness" / "codex-privileged-rpc-inventory.json"
)


def load_inventory() -> dict[str, object]:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def test_checked_in_inventory_passes_real_cli() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_codex_rpc_inventory.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Codex RPC inventory valid:")
    assert "0 proposed registrations" in result.stdout


def test_default_disposition_must_remain_deny() -> None:
    inventory = load_inventory()
    inventory["default_disposition"] = "reimplement_behind_atlas_gate"

    try:
        validate_inventory(inventory)
    except InventoryError as error:
        assert "default_disposition must be deny" in str(error)
    else:
        raise AssertionError("unsafe default disposition unexpectedly passed")


def test_critical_shell_method_must_remain_denied() -> None:
    inventory = load_inventory()
    inventory["groups"][0]["disposition"] = "reimplement_behind_atlas_gate"

    try:
        validate_inventory(inventory)
    except InventoryError as error:
        assert "critical methods must remain denied" in str(error)
        assert "thread/shellCommand" in str(error)
    else:
        raise AssertionError("raw shell method unexpectedly passed")


def test_duplicate_method_classification_is_rejected() -> None:
    inventory = load_inventory()
    duplicate_group = copy.deepcopy(inventory["groups"][0])
    duplicate_group["id"] = "duplicate-shell"
    inventory["groups"].append(duplicate_group)

    try:
        validate_inventory(inventory)
    except InventoryError as error:
        assert "duplicate method classification" in str(error)
    else:
        raise AssertionError("duplicate RPC classification unexpectedly passed")


def test_denied_or_unclassified_registration_is_rejected() -> None:
    method_dispositions = validate_inventory(load_inventory())

    for method in ["process/spawn", "unknown/newMethod"]:
        try:
            validate_registration([method], method_dispositions)
        except InventoryError as error:
            assert method in str(error)
        else:
            raise AssertionError(f"unsafe registration unexpectedly passed: {method}")


def test_gated_atlas_reimplementation_may_be_registered() -> None:
    method_dispositions = validate_inventory(load_inventory())

    validate_registration(["thread/start", "turn/start"], method_dispositions)
