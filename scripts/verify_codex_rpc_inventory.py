"""Verify that Atlas cannot register raw privilege-bearing Codex RPC methods."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = (
    ROOT / "docs" / "harness" / "codex-privileged-rpc-inventory.json"
)

GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TOOLCHAIN_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
METHOD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:[/][A-Za-z0-9_]+)*$")
EXPECTED_GATES = {
    "authenticated_principal",
    "workspace_authorization",
    "schema_and_size_validation",
    "idempotency_and_expected_sequence",
    "policy_and_budget",
    "sandbox_or_egress",
    "result_validation_and_redaction",
    "durable_audit",
}
REQUIRED_DENIED_METHODS = {
    "thread/shellCommand",
    "command/exec",
    "command/exec/write",
    "command/exec/terminate",
    "command/exec/resize",
    "process/spawn",
    "process/writeStdin",
    "process/kill",
    "process/resizePty",
    "fs/readFile",
    "fs/writeFile",
    "fs/createDirectory",
    "fs/getMetadata",
    "fs/readDirectory",
    "fs/remove",
    "fs/copy",
    "fs/watch",
    "fs/unwatch",
}


class InventoryError(ValueError):
    """The RPC inventory violates an Atlas security invariant."""


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InventoryError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise InventoryError(f"{field} must be a non-empty list")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"{field} must be a non-empty string")
    return value


def _require_date(value: Any, field: str) -> None:
    raw_date = _require_string(value, field)
    try:
        date.fromisoformat(raw_date)
    except ValueError as error:
        raise InventoryError(f"{field} must be an ISO-8601 date") from error


def _validate_upstream(value: Any) -> None:
    upstream = _require_mapping(value, "upstream")
    _require_string(upstream.get("repository"), "upstream.repository")
    commit = _require_string(upstream.get("commit"), "upstream.commit")
    toolchain = _require_string(
        upstream.get("rust_toolchain"), "upstream.rust_toolchain"
    )
    if not GIT_COMMIT_PATTERN.fullmatch(commit):
        raise InventoryError("upstream.commit must be a full lowercase Git commit")
    if not TOOLCHAIN_PATTERN.fullmatch(toolchain):
        raise InventoryError("upstream.rust_toolchain must be an exact version")

    metrics = _require_mapping(
        upstream.get("source_metrics"), "upstream.source_metrics"
    )
    for metric_name, metric_value in metrics.items():
        if not isinstance(metric_value, int) or metric_value <= 0:
            raise InventoryError(
                f"upstream.source_metrics.{metric_name} must be a positive integer"
            )


def validate_inventory(payload: Any) -> dict[str, str]:
    inventory = _require_mapping(payload, "inventory")
    if inventory.get("schema_version") != 1:
        raise InventoryError("schema_version must be 1")
    _require_date(inventory.get("reviewed_at"), "reviewed_at")
    _validate_upstream(inventory.get("upstream"))
    if inventory.get("default_disposition") != "deny":
        raise InventoryError("default_disposition must be deny")

    dispositions = set(
        _require_list(inventory.get("allowed_dispositions"), "allowed_dispositions")
    )
    if dispositions != {"deny", "reimplement_behind_atlas_gate"}:
        raise InventoryError("allowed_dispositions must contain only approved values")
    gates = set(_require_list(inventory.get("required_gates"), "required_gates"))
    if gates != EXPECTED_GATES:
        raise InventoryError("required_gates does not match the Atlas boundary")

    method_dispositions: dict[str, str] = {}
    group_ids: set[str] = set()
    groups = _require_list(inventory.get("groups"), "groups")
    for group_index, group_value in enumerate(groups):
        prefix = f"groups[{group_index}]"
        group = _require_mapping(group_value, prefix)
        group_id = _require_string(group.get("id"), f"{prefix}.id")
        if group_id in group_ids:
            raise InventoryError(f"duplicate group id: {group_id}")
        group_ids.add(group_id)

        disposition = _require_string(
            group.get("disposition"), f"{prefix}.disposition"
        )
        if disposition not in dispositions:
            raise InventoryError(f"{prefix}.disposition is invalid")
        _require_string(group.get("reason"), f"{prefix}.reason")
        _require_string(group.get("atlas_replacement"), f"{prefix}.atlas_replacement")
        evidence = _require_list(group.get("evidence"), f"{prefix}.evidence")
        for evidence_index, evidence_item in enumerate(evidence):
            _require_string(evidence_item, f"{prefix}.evidence[{evidence_index}]")

        methods = _require_list(group.get("methods"), f"{prefix}.methods")
        for method_index, method_value in enumerate(methods):
            method = _require_string(method_value, f"{prefix}.methods[{method_index}]")
            if not METHOD_PATTERN.fullmatch(method):
                raise InventoryError(f"{prefix}.methods[{method_index}] is invalid")
            if method in method_dispositions:
                raise InventoryError(f"duplicate method classification: {method}")
            method_dispositions[method] = disposition

    missing_denials = sorted(
        method
        for method in REQUIRED_DENIED_METHODS
        if method_dispositions.get(method) != "deny"
    )
    if missing_denials:
        raise InventoryError(
            "critical methods must remain denied: " + ", ".join(missing_denials)
        )
    _require_string(inventory.get("registration_rule"), "registration_rule")
    return method_dispositions


def _load_registered_methods(path: Path) -> list[str]:
    with path.open(encoding="utf-8") as registered_file:
        payload = json.load(registered_file)
    if isinstance(payload, dict):
        payload = payload.get("methods")
    methods = _require_list(payload, "registered_methods")
    return [
        _require_string(method, f"registered_methods[{index}]")
        for index, method in enumerate(methods)
    ]


def validate_registration(
    registered_methods: list[str], method_dispositions: dict[str, str]
) -> None:
    for method in registered_methods:
        disposition = method_dispositions.get(method, "deny")
        if disposition != "reimplement_behind_atlas_gate":
            raise InventoryError(
                f"registered method is denied or unclassified: {method}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the pinned Codex RPC inventory and registration gate."
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--registered-methods", type=Path)
    arguments = parser.parse_args()

    try:
        with arguments.inventory.open(encoding="utf-8") as inventory_file:
            inventory = json.load(inventory_file)
        method_dispositions = validate_inventory(inventory)
        registered_count = 0
        if arguments.registered_methods is not None:
            registered_methods = _load_registered_methods(
                arguments.registered_methods
            )
            validate_registration(registered_methods, method_dispositions)
            registered_count = len(registered_methods)
    except (OSError, json.JSONDecodeError, InventoryError) as error:
        print(f"Codex RPC inventory verification failed: {error}", file=sys.stderr)
        return 1

    denied_count = sum(
        disposition == "deny" for disposition in method_dispositions.values()
    )
    reimplemented_count = len(method_dispositions) - denied_count
    print(
        "Codex RPC inventory valid: "
        f"{denied_count} denied, {reimplemented_count} gated, "
        f"{registered_count} proposed registrations"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
