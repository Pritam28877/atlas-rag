"""Unimplemented parser and extension hosts remain unreachable."""

import json
from pathlib import Path

REGISTRY_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs/harness/python-privileged-operation-registry.json"
)
EXPECTED_DISABLED_OPERATIONS = {
    "harness.document.parse",
    "harness.extension.mcp.remote.invoke",
    "harness.extension.mcp.start",
    "harness.extension.plugin.start",
}


def test_parser_mcp_and_plugin_hosts_are_planned_disabled() -> None:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    operations = {
        operation["id"]: operation
        for operation in payload["operations"]
        if operation["id"] in EXPECTED_DISABLED_OPERATIONS
    }

    assert set(operations) == EXPECTED_DISABLED_OPERATIONS
    assert all(
        operation["registration_state"] == "planned_disabled"
        and operation["implementation"] is None
        for operation in operations.values()
    )
