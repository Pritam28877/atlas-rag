"""Validate fixture metadata, local artifact hashes, and the JSON schema."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "docs" / "benchmarks" / "fixture-manifest.json"
SCHEMA_PATH = ROOT / "docs" / "benchmarks" / "fixture-manifest.schema.json"


def sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(manifest)

    for fixture in manifest["fixtures"]:
        artifact = fixture["artifact"]
        path = ROOT / artifact["object_ref"]
        if not path.is_file():
            raise SystemExit(f"Missing fixture: {path}")
        if artifact["bytes"] != path.stat().st_size:
            raise SystemExit(f"Byte-size mismatch: {fixture['id']}")
        if artifact["sha256"] != sha256(path):
            raise SystemExit(f"SHA-256 mismatch: {fixture['id']}")
    print(f"Validated {len(manifest['fixtures'])} fixture records.")


if __name__ == "__main__":
    main()
