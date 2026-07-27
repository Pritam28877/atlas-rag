#!/usr/bin/env python3
"""Verify privileged callsite classification and enforcement evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.harness_privileged_reachability import (
    ReachabilityScanError,
    defined_symbols,
    discover_privileged_calls,
)
from scripts.verify_harness_privileged_operations import (
    DEFAULT_REGISTRY,
    GATES,
    RegistryError,
    validate_registry,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REACHABILITY = (
    ROOT / "docs/harness/python-privileged-reachability.json"
)
EXPOSURES = {
    "authenticated_read_only",
    "internal_guarded",
    "opt_in_cli",
    "registered",
}
REACHABILITY_FIELDS = {"schema_version", "scan_roots", "callsites"}
CALLSITE_FIELDS = {
    "id",
    "path",
    "symbol",
    "sink",
    "ordinal",
    "exposure",
    "operation_id",
    "enforced_gates",
    "missing_gates",
    "evidence",
}
READ_ONLY_GATES = {"authentication", "schema_validation", "result_filter"}
MAXIMUM_CALLSITES = 128


class ReachabilityError(ValueError):
    """A privileged callsite is unclassified or lacks gate evidence."""


def validate_reachability(
    payload: Any,
    registry_payload: Any,
    *,
    root: Path = ROOT,
) -> tuple[int, int]:
    document = _mapping(payload, "reachability")
    if set(document) != REACHABILITY_FIELDS:
        raise ReachabilityError(
            "reachability has missing or unknown fields"
        )
    if document["schema_version"] != 1:
        raise ReachabilityError("reachability schema_version must be 1")

    scan_roots = _scan_roots(document["scan_roots"])
    registry_operations = _registry_operations(registry_payload, root=root)
    raw_callsites = document["callsites"]
    if (
        not isinstance(raw_callsites, list)
        or not raw_callsites
        or len(raw_callsites) > MAXIMUM_CALLSITES
    ):
        raise ReachabilityError("callsites must be a bounded non-empty list")

    manifest_ids: list[str] = []
    registered_count = 0
    for index, value in enumerate(raw_callsites):
        callsite = _mapping(value, f"callsites[{index}]")
        if set(callsite) != CALLSITE_FIELDS:
            raise ReachabilityError(
                f"callsites[{index}] has missing or unknown fields"
            )
        callsite_id = _string(callsite["id"], f"callsites[{index}].id")
        if callsite_id != _expected_callsite_id(callsite):
            raise ReachabilityError("callsite id does not match its fields")
        manifest_ids.append(callsite_id)
        registered_count += _validate_classification(
            callsite,
            registry_operations,
            root=root,
        )
    if manifest_ids != sorted(set(manifest_ids)):
        raise ReachabilityError("callsites must be uniquely sorted by id")

    try:
        discovered_ids = {
            call.callsite_id
            for call in discover_privileged_calls(scan_roots, root=root)
        }
    except ReachabilityScanError as error:
        raise ReachabilityError(str(error)) from error
    missing = sorted(discovered_ids - set(manifest_ids))
    stale = sorted(set(manifest_ids) - discovered_ids)
    if missing:
        raise ReachabilityError(
            f"unclassified privileged callsite: {missing[0]}"
        )
    if stale:
        raise ReachabilityError(f"stale privileged callsite: {stale[0]}")
    return len(discovered_ids), registered_count


def _validate_classification(
    callsite: dict[str, Any],
    registry_operations: dict[str, dict[str, Any]],
    *,
    root: Path,
) -> int:
    exposure = _string(callsite["exposure"], "callsite.exposure")
    if exposure not in EXPOSURES:
        raise ReachabilityError("callsite exposure is invalid")
    enforced_gates = _string_set(
        callsite["enforced_gates"],
        "callsite.enforced_gates",
    )
    missing_gates = _string_set(
        callsite["missing_gates"],
        "callsite.missing_gates",
        allow_empty=True,
    )
    if (
        not enforced_gates.issubset(GATES)
        or not missing_gates.issubset(GATES)
        or enforced_gates.intersection(missing_gates)
    ):
        raise ReachabilityError("callsite contains an unknown gate")
    evidence = _mapping(callsite["evidence"], "callsite.evidence")
    if set(evidence) != enforced_gates:
        raise ReachabilityError("callsite gate evidence is incomplete")
    for gate, reference in evidence.items():
        _validate_evidence_reference(reference, gate=gate, root=root)

    operation_id = callsite["operation_id"]
    if operation_id is None:
        if (
            exposure != "authenticated_read_only"
            or enforced_gates != READ_ONLY_GATES
            or missing_gates
        ):
            raise ReachabilityError(
                "only fully gated read-only routes may omit operation id"
            )
        return 0

    operation = registry_operations.get(
        _string(operation_id, "callsite.operation_id")
    )
    if operation is None:
        raise ReachabilityError("callsite references an unknown operation")
    operation_gates = set(operation["required_gates"])
    if enforced_gates.union(missing_gates) != operation_gates:
        raise ReachabilityError("callsite does not account for operation gates")
    if exposure == "registered":
        if (
            operation["registration_state"] != "registered"
            or missing_gates
            or operation["implementation"] != callsite["path"]
        ):
            raise ReachabilityError("registered callsite is not fully mapped")
        return 1
    if exposure not in {"internal_guarded", "opt_in_cli"}:
        raise ReachabilityError("operation-backed exposure is invalid")
    if operation["registration_state"] != "planned_disabled":
        raise ReachabilityError("non-registered callsite must remain disabled")
    return 0


def _registry_operations(
    payload: Any,
    *,
    root: Path,
) -> dict[str, dict[str, Any]]:
    try:
        validate_registry(payload, root=root)
    except RegistryError as error:
        raise ReachabilityError("privileged registry is invalid") from error
    registry = _mapping(payload, "registry")
    return {
        operation["id"]: operation
        for operation in registry["operations"]
    }


def _scan_roots(value: Any) -> tuple[PurePosixPath, ...]:
    root_values = _string_list(value, "scan_roots")
    roots = tuple(PurePosixPath(item) for item in root_values)
    if tuple(sorted(set(roots))) != roots:
        raise ReachabilityError("scan_roots must be unique and sorted")
    for index, scan_root in enumerate(roots):
        if (
            scan_root.is_absolute()
            or ".." in scan_root.parts
            or not scan_root.parts
            or scan_root.parts[0] != "app"
        ):
            raise ReachabilityError("scan_roots must stay under app/")
        if any(scan_root.is_relative_to(parent) for parent in roots[:index]):
            raise ReachabilityError("scan_roots must not overlap")
    return roots


def _expected_callsite_id(callsite: dict[str, Any]) -> str:
    path_text = _string(callsite["path"], "callsite.path")
    path = PurePosixPath(path_text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "app"
        or path.suffix != ".py"
    ):
        raise ReachabilityError("callsite path is invalid")
    symbol = _string(callsite["symbol"], "callsite.symbol")
    sink = _string(callsite["sink"], "callsite.sink")
    ordinal = callsite["ordinal"]
    if not isinstance(ordinal, int) or not 1 <= ordinal <= 32:
        raise ReachabilityError("callsite ordinal is invalid")
    return f"{path_text}#{symbol}:{sink}:{ordinal}"


def _validate_evidence_reference(
    value: Any,
    *,
    gate: str,
    root: Path,
) -> None:
    reference = _string(value, f"evidence.{gate}")
    path_text, separator, symbol = reference.partition("#")
    path = PurePosixPath(path_text)
    if (
        not separator
        or not symbol
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "app"
        or path.suffix != ".py"
    ):
        raise ReachabilityError("gate evidence reference is invalid")
    source_path = root / path
    if not source_path.is_file():
        raise ReachabilityError("gate evidence path does not exist")
    try:
        symbols = defined_symbols(source_path)
    except ReachabilityScanError as error:
        raise ReachabilityError(str(error)) from error
    if symbol not in symbols:
        raise ReachabilityError(
            f"gate evidence symbol does not exist: {reference}"
        )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReachabilityError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ReachabilityError(f"{field} must be a bounded string")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ReachabilityError(f"{field} must be a non-empty list")
    values = [_string(item, f"{field}[]") for item in value]
    if len(values) != len(set(values)):
        raise ReachabilityError(f"{field} must contain unique values")
    return values


def _string_set(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
) -> set[str]:
    if allow_empty and value == []:
        return set()
    return set(_string_list(value, field))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify direct privileged Python sink reachability."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_REACHABILITY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    arguments = parser.parse_args()
    try:
        manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
        registry = json.loads(arguments.registry.read_text(encoding="utf-8"))
        callsite_count, registered_count = validate_reachability(
            manifest,
            registry,
        )
    except (OSError, json.JSONDecodeError, ReachabilityError) as error:
        print(f"Privileged reachability failed: {error}", file=sys.stderr)
        return 1
    print(
        "Privileged reachability valid: "
        f"{callsite_count} callsites, {registered_count} registered"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
