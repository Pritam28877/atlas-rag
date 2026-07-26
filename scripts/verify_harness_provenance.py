"""Fail closed when Atlas Harness provenance permits unsafe source reuse."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "harness" / "provenance-manifest.json"

GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PERMISSIVE_LICENSES = {"Apache-2.0", "MIT"}
COPY_TREATMENTS = {"base_and_adapt", "pattern_adaptation"}
NO_COPY_TREATMENTS = {"behavior_spec_only", "concept_only"}
LICENSE_STATUSES = {"verified", "restricted", "unverified"}
REVISION_KINDS = {"git_commit", "documentation_snapshot_date"}


class ManifestError(ValueError):
    """A provenance invariant was violated."""


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str, *, allow_empty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ManifestError(f"{field} must be {qualifier}")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    return value


def _require_date(value: Any, field: str) -> str:
    raw_date = _require_string(value, field)
    try:
        date.fromisoformat(raw_date)
    except ValueError as error:
        raise ManifestError(f"{field} must be an ISO-8601 date") from error
    return raw_date


def _validate_outbound_license(value: Any) -> None:
    outbound = _require_mapping(value, "outbound_license")
    proposal = _require_string(outbound.get("proposal"), "outbound_license.proposal")
    status = _require_string(outbound.get("status"), "outbound_license.status")
    approval_required = outbound.get("approval_required")

    if proposal != "Apache-2.0":
        raise ManifestError("outbound_license.proposal must be Apache-2.0")
    if status not in {"pending_human_approval", "approved"}:
        raise ManifestError("outbound_license.status is invalid")
    if not isinstance(approval_required, bool):
        raise ManifestError("outbound_license.approval_required must be a boolean")
    if status == "pending_human_approval" and not approval_required:
        raise ManifestError("pending outbound license must require human approval")

    notice_plan = _require_list(
        outbound.get("notice_plan"), "outbound_license.notice_plan"
    )
    for index, notice in enumerate(notice_plan):
        _require_string(notice, f"outbound_license.notice_plan[{index}]")


def _validate_source(value: Any, index: int) -> tuple[str, dict[str, Any]]:
    prefix = f"sources[{index}]"
    source = _require_mapping(value, prefix)
    source_id = _require_string(source.get("id"), f"{prefix}.id")
    _require_string(source.get("name"), f"{prefix}.name")
    _require_string(source.get("kind"), f"{prefix}.kind")
    _require_string(source.get("location"), f"{prefix}.location")
    revision = _require_string(source.get("revision"), f"{prefix}.revision")
    revision_kind = _require_string(
        source.get("revision_kind"), f"{prefix}.revision_kind"
    )
    _require_date(source.get("reviewed_at"), f"{prefix}.reviewed_at")
    _require_string(source.get("owner"), f"{prefix}.owner")

    if revision_kind not in REVISION_KINDS:
        raise ManifestError(f"{prefix}.revision_kind is invalid")
    if revision_kind == "git_commit" and not GIT_COMMIT_PATTERN.fullmatch(revision):
        raise ManifestError(f"{prefix}.revision must be a full lowercase Git commit")
    if revision_kind == "documentation_snapshot_date":
        _require_date(revision, f"{prefix}.revision")

    license_record = _require_mapping(source.get("license"), f"{prefix}.license")
    license_status = _require_string(
        license_record.get("status"), f"{prefix}.license.status"
    )
    if license_status not in LICENSE_STATUSES:
        raise ManifestError(f"{prefix}.license.status is invalid")
    license_spdx = license_record.get("spdx")
    if license_spdx is not None:
        _require_string(license_spdx, f"{prefix}.license.spdx")
    evidence = _require_list(
        license_record.get("evidence"), f"{prefix}.license.evidence"
    )
    for evidence_index, item in enumerate(evidence):
        _require_string(item, f"{prefix}.license.evidence[{evidence_index}]")

    usage = _require_mapping(source.get("usage"), f"{prefix}.usage")
    treatment = _require_string(usage.get("treatment"), f"{prefix}.usage.treatment")
    copy_allowed = usage.get("copy_allowed")
    if not isinstance(copy_allowed, bool):
        raise ManifestError(f"{prefix}.usage.copy_allowed must be a boolean")
    conditions = _require_list(usage.get("conditions"), f"{prefix}.usage.conditions")
    for condition_index, condition in enumerate(conditions):
        _require_string(condition, f"{prefix}.usage.conditions[{condition_index}]")

    if copy_allowed:
        if license_status != "verified" or license_spdx not in PERMISSIVE_LICENSES:
            raise ManifestError(
                f"{prefix} cannot allow copying without a verified permissive license"
            )
        if treatment not in COPY_TREATMENTS:
            raise ManifestError(f"{prefix} has an invalid copy treatment")
    elif treatment not in NO_COPY_TREATMENTS:
        raise ManifestError(f"{prefix} must use a non-copy treatment")

    return source_id, source


def _validate_adapted_file(
    value: Any,
    index: int,
    sources_by_id: dict[str, dict[str, Any]],
) -> str:
    prefix = f"adapted_files[{index}]"
    adapted_file = _require_mapping(value, prefix)
    path = _require_string(adapted_file.get("path"), f"{prefix}.path")
    source_id = _require_string(adapted_file.get("source_id"), f"{prefix}.source_id")
    source_path = _require_string(
        adapted_file.get("source_path"), f"{prefix}.source_path"
    )
    source_revision = _require_string(
        adapted_file.get("source_revision"), f"{prefix}.source_revision"
    )
    adapted_license = _require_string(
        adapted_file.get("license"), f"{prefix}.license"
    )
    _require_string(adapted_file.get("change_summary"), f"{prefix}.change_summary")
    _require_string(adapted_file.get("owner"), f"{prefix}.owner")

    normalized_path = PurePosixPath(path)
    if normalized_path.is_absolute() or ".." in normalized_path.parts:
        raise ManifestError(f"{prefix}.path must stay inside the repository")
    normalized_source_path = PurePosixPath(source_path)
    if normalized_source_path.is_absolute() or ".." in normalized_source_path.parts:
        raise ManifestError(f"{prefix}.source_path must be repository-relative")

    source = sources_by_id.get(source_id)
    if source is None:
        raise ManifestError(f"{prefix} references unknown source {source_id}")
    if not source["usage"]["copy_allowed"]:
        raise ManifestError(f"{prefix} copies from source that forbids copying")
    if source_revision != source["revision"]:
        raise ManifestError(f"{prefix}.source_revision does not match source")
    if adapted_license != source["license"]["spdx"]:
        raise ManifestError(f"{prefix}.license does not match source license")
    return path


def validate_manifest(payload: Any) -> tuple[int, int]:
    manifest = _require_mapping(payload, "manifest")
    if manifest.get("schema_version") != 1:
        raise ManifestError("schema_version must be 1")
    _require_date(manifest.get("reviewed_at"), "reviewed_at")
    _validate_outbound_license(manifest.get("outbound_license"))

    sources = _require_list(manifest.get("sources"), "sources")
    sources_by_id: dict[str, dict[str, Any]] = {}
    for index, source_value in enumerate(sources):
        source_id, source = _validate_source(source_value, index)
        if source_id in sources_by_id:
            raise ManifestError(f"duplicate source id: {source_id}")
        sources_by_id[source_id] = source

    adapted_files = _require_list(
        manifest.get("adapted_files"), "adapted_files", allow_empty=True
    )
    adapted_paths: set[str] = set()
    for index, adapted_value in enumerate(adapted_files):
        path = _validate_adapted_file(adapted_value, index, sources_by_id)
        if path in adapted_paths:
            raise ManifestError(f"duplicate adapted file path: {path}")
        adapted_paths.add(path)

    return len(sources_by_id), len(adapted_paths)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Atlas Harness provenance and source-reuse invariants."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args()

    try:
        with arguments.manifest.open(encoding="utf-8") as manifest_file:
            payload = json.load(manifest_file)
        source_count, adapted_file_count = validate_manifest(payload)
    except (OSError, json.JSONDecodeError, ManifestError) as error:
        print(f"Provenance verification failed: {error}", file=sys.stderr)
        return 1

    print(
        "Provenance manifest valid: "
        f"{source_count} sources, {adapted_file_count} adapted files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
