"""Validate fixture metadata, artifact integrity, and corpus semantics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "benchmarks" / "fixtures"
MANIFEST_PATH = ROOT / "docs" / "benchmarks" / "fixture-manifest.json"
SCHEMA_PATH = ROOT / "docs" / "benchmarks" / "fixture-manifest.schema.json"
READY_STATES = {"READY", "READY_WITH_WARNINGS"}


def sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def manifest_path(reference: str) -> Path:
    path = (ROOT / reference).resolve()
    if ROOT not in path.parents:
        raise ValueError(f"Artifact reference escapes the repository: {reference}")
    return path


def validate_fixture(fixture: dict[str, Any]) -> None:
    artifact = fixture["artifact"]
    path = manifest_path(artifact["object_ref"])
    if not path.is_file():
        raise ValueError(f"Missing fixture: {path}")
    if artifact["status"] != "verified":
        raise ValueError(f"Fixture is not verified: {fixture['id']}")
    if artifact["bytes"] != path.stat().st_size:
        raise ValueError(f"Byte-size mismatch: {fixture['id']}")
    if artifact["sha256"] != sha256(path):
        raise ValueError(f"SHA-256 mismatch: {fixture['id']}")

    expected = fixture["expected_outcome"]
    golden_ref = expected["golden_ref"]
    if expected["terminal_state"] in READY_STATES:
        if golden_ref is None:
            raise ValueError(f"Ready fixture has no golden: {fixture['id']}")
        golden = json.loads(manifest_path(golden_ref).read_text(encoding="utf-8"))
        if golden["fixture_id"] != fixture["id"]:
            raise ValueError(f"Golden fixture ID mismatch: {fixture['id']}")
        if len(golden["pages"]) != artifact["page_count"]:
            raise ValueError(f"Golden page count mismatch: {fixture['id']}")
    elif golden_ref is not None:
        raise ValueError(f"Terminal fixture has a golden: {fixture['id']}")

    if expected["terminal_state"] in READY_STATES:
        reader = PdfReader(path)
        if len(reader.pages) != artifact["page_count"]:
            raise ValueError(f"PDF page count mismatch: {fixture['id']}")


def load_valid_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(manifest)

    fixtures = manifest["fixtures"]
    identifiers = [fixture["id"] for fixture in fixtures]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Fixture IDs must be unique")
    by_id = {fixture["id"]: fixture for fixture in fixtures}
    paths = {manifest_path(fixture["artifact"]["object_ref"]) for fixture in fixtures}
    actual_paths = set(FIXTURES_DIR.glob("*.pdf"))
    if paths != actual_paths:
        raise ValueError("Manifest fixture paths do not match local PDF fixtures")
    for fixture in fixtures:
        duplicate_of = fixture.get("duplicate_of")
        if duplicate_of is not None and duplicate_of not in by_id:
            raise ValueError(f"Unknown duplicate source: {fixture['id']}")
        validate_fixture(fixture)
    return manifest


def main() -> None:
    manifest = load_valid_manifest()
    print(f"Validated {len(manifest['fixtures'])} fixture records.")


if __name__ == "__main__":
    main()
